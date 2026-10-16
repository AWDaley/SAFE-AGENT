"""W3 result evidence, atomicity and recovery; all inference is synthetic."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.exc import DBAPIError

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import RequestClarification
from safe_agent.contracts.inference_results import ValidatedInferenceResult
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.inference_dispatch import InferenceDispatchDenied
from safe_agent.storage.inference_results import (
    InferenceResultDenied,
    _read_record,
    recover_validated_result,
)
from safe_agent.storage.models import InferenceInvocationEventRow, InferenceInvocationRow, OutboxRow
from safe_agent.storage.repository import LedgerRepository
from safe_agent.storage.result_models import InferenceValidatedResultRow as ResultRow
from tests.m7.admission_fixtures import policy
from tests.m7.test_inference_coordinator import configured, held, transport


@pytest.fixture
def repo(tmp_path):
    value = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'results.sqlite'}")
    yield value
    value.close()


def completed(repo, *, reply_transform=None):
    args, ticks, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    reply = OfflineMockInferenceTransport().infer(request)
    if reply_transform is not None:
        reply = reply_transform(reply)
    view, outcome = store.finish(*args, claim, reply)
    return args, ticks, admission, store, request, view, outcome


def values(row):
    return {c.name: getattr(row, c.name) for c in ResultRow.__table__.columns}


def test_exact_result_snapshot_recovery_has_no_effect_or_budget_release(repo):
    args, _, admission, _, request, view, outcome = completed(repo)
    before = held(repo)
    result = TrustedInferenceCoordinator(admission, transport()).resume(*args[:2])
    assert result.status == "AVAILABLE"
    assert result.outcome == outcome and result.source_snapshot == request
    assert result.invocation == view
    assert result.business_execution_authorized is False
    assert result.billing_verified is False and result.dispatched_this_call is False
    assert "source_snapshot" not in result.model_dump_json()
    with repo._sessions() as db:
        row = db.scalars(select(ResultRow)).one()
        record = _read_record(row)
        assert record.request == request and record.outcome == outcome
        assert record.lease == args[1]
        assert record.result_hash == result.result_hash
        assert record.snapshot_hash == sha256({"task": request.task, "availability": request.availability})
        assert record.outcome.proposal.simulation_ref == "simulation_pending"
        assert "output_json" not in row.record_json
        assert '"instruction"' not in row.record_json
        assert '"token"' not in row.record_json
        assert db.scalars(select(OutboxRow)).all() == []
    assert held(repo) == before
    assert len(repo.read_events(args[1].task_id)) == 1


def test_expired_result_is_metadata_only_and_never_redispatched(repo, monkeypatch):
    args, ticks, admission, _, _, _, _ = completed(repo)
    before = held(repo)
    ticks[0] += timedelta(seconds=11)
    wire = transport()
    calls = []
    monkeypatch.setattr(wire, "send", lambda *a: calls.append(a))
    runtime = TrustedInferenceCoordinator(admission, wire)
    recovered = runtime.resume(*args[:2])
    assert recovered.status == "STALE" and recovered.outcome is None
    assert recovered.source_snapshot is None and recovered.result_hash is not None
    assert runtime.execute(*args).outcome is None
    assert calls == [] and held(repo) == before


@pytest.mark.parametrize("change", ["tenant", "actor", "scope", "expired_actor", "attempt",
                                    "workspace", "manifest", "attempt_number"])
def test_recovery_rejects_foreign_or_forged_identity(repo, change):
    args, ticks, admission, _, _, _, _ = completed(repo)
    actor, lease, _ = args
    if change == "tenant":
        actor = actor.model_copy(update={"organization_id": "org_foreign"})
    elif change == "actor":
        actor = actor.model_copy(update={"actor_id": "other_actor"})
    elif change == "scope":
        actor = actor.model_copy(update={"scopes": ("task:read",)})
    elif change == "expired_actor":
        actor = actor.model_copy(update={"authenticated_until": ticks[0]})
    elif change == "attempt":
        lease = lease.model_copy(update={"attempt_id": "attempt_forged"})
    elif change == "workspace":
        lease = lease.model_copy(update={"workspace_version": lease.workspace_version + 1})
    elif change == "attempt_number":
        lease = lease.model_copy(update={"attempt_number": lease.attempt_number + 1})
    else:
        lease = lease.model_copy(update={"manifest_hash": "sha256:" + "a" * 64})
    with pytest.raises(InferenceResultDenied, match="^INFERENCE_RESULT_DENIED$"):
        recover_validated_result(admission, actor, lease)


def test_changed_prompt_policy_or_regressed_clock_cannot_recover(repo):
    args, ticks, admission, _, _, _, _ = completed(repo)
    with pytest.raises(InferenceResultDenied):
        recover_validated_result(admission, *args[:2], expected_prompt=args[2] + " ")
    changed = policy().model_copy(update={"route": policy().route.model_copy(update={"model_revision": "other"})})
    with pytest.raises(InferenceResultDenied):
        recover_validated_result(TrustedInferenceAdmission(repo, policy=changed, clock=lambda: ticks[0]), *args[:2])
    ticks[0] -= timedelta(seconds=1)
    with pytest.raises(InferenceResultDenied):
        recover_validated_result(admission, *args[:2])


def test_update_delete_and_duplicate_insert_are_blocked_by_database(repo):
    args, _, admission, _, _, _, _ = completed(repo)
    before = recover_validated_result(admission, *args[:2])
    with repo._sessions() as db:
        original = values(db.scalars(select(ResultRow)).one())
    for stmt in (update(ResultRow).values(record_json="{}"), delete(ResultRow),
                 insert(ResultRow).values(**original),
                 insert(ResultRow).values(**{**original, "record_json": "{}"})):
        with pytest.raises(DBAPIError):
            with repo.engine.begin() as conn:
                conn.execute(stmt)
    assert recover_validated_result(admission, *args[:2]) == before


@pytest.mark.parametrize("change", ["candidate", "snapshot", "digest", "binding", "oversized", "duplicate_key"])
def test_corrupted_payload_or_denormalized_identity_fails_closed(repo, change):
    completed(repo)
    with repo._sessions() as db:
        original = values(db.scalars(select(ResultRow)).one())
    raw = json.loads(original["record_json"])
    if change == "candidate":
        raw["outcome"]["proposal"]["calendar_ref"] = "ref_other"
    elif change == "snapshot":
        raw["request"]["availability"]["revision"] = "other_snapshot"
    elif change == "digest":
        raw["result_hash"] = "sha256:" + "0" * 64
    elif change == "binding":
        original["task_id"] = "other_task"
    elif change == "oversized":
        original["record_json"] = "x" * 65_537
    elif change == "duplicate_key":
        original["record_json"] = original["record_json"].replace(
            '"policy_hash":', '"policy_hash":"duplicate","policy_hash":', 1,
        )
    if change in {"candidate", "snapshot", "digest"}:
        original["record_json"] = canonical_json(raw).decode()
    with pytest.raises(ValueError):
        _read_record(ResultRow(**original))


def test_result_insert_failure_rolls_back_validation_and_prevents_resend(repo, monkeypatch):
    args, ticks, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    before = held(repo)

    def fail_insert(mapper, connection, target):
        raise RuntimeError("PRIVATE_INSERT_ERROR")

    event.listen(ResultRow, "before_insert", fail_insert)
    try:
        with pytest.raises(InferenceDispatchDenied, match="^INFERENCE_DISPATCH_DENIED$"):
            store.finish(*args, claim, OfflineMockInferenceTransport().infer(request))
    finally:
        event.remove(ResultRow, "before_insert", fail_insert)
    with repo._sessions() as db:
        assert db.get(InferenceInvocationRow, request.request_id).state == "DISPATCH_STARTED"
        assert db.get(InferenceInvocationEventRow, (request.request_id, 3)) is None
        assert db.get(ResultRow, request.request_id) is None
    wire = transport()
    calls = []
    monkeypatch.setattr(wire, "send", lambda *a: calls.append(a))
    ticks[0] += timedelta(seconds=11)
    answer = TrustedInferenceCoordinator(admission, wire).execute(*args)
    assert answer.invocation.state == "UNKNOWN" and answer.outcome is None
    assert calls == [] and held(repo) == before


def test_parallel_recovery_returns_same_record_without_duplicate_rows(repo):
    args, _, admission, _, _, _, _ = completed(repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: recover_validated_result(admission, *args[:2]), range(8)))
    assert all(r == results[0] and r.status == "AVAILABLE" for r in results)
    with repo._sessions() as db:
        assert len(db.scalars(select(ResultRow)).all()) == 1


def test_clarification_persists_only_fixed_safe_text(repo):
    unsafe = RequestClarification(reason_code="INSUFFICIENT_CONTEXT", question="PRIVATE_MODEL_INJECTION").model_dump_json()
    args, _, admission, _, _, _, _ = completed(repo, reply_transform=lambda r: r.model_copy(update={"output_json": unsafe}))
    with repo._sessions() as db:
        raw = db.scalars(select(ResultRow.record_json)).one()
        assert "PRIVATE_MODEL_INJECTION" not in raw
        assert ValidatedInferenceResult.model_validate_json(raw).outcome.proposal.reason_code == "INSUFFICIENT_CONTEXT"
    assert recover_validated_result(admission, *args[:2]).status == "AVAILABLE"


def test_slow_recovery_commit_cannot_return_expired_candidate(repo):
    args, ticks, admission, _, _, _, _ = completed(repo)

    def slow_commit(session):
        ticks[0] += timedelta(seconds=11)

    event.listen(repo._sessions, "before_commit", slow_commit)
    try:
        result = recover_validated_result(admission, *args[:2])
    finally:
        event.remove(repo._sessions, "before_commit", slow_commit)
    assert result.status == "STALE" and result.outcome is None
