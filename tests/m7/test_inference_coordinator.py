"""Trusted offline dispatch: fenced claims, one-way markers and crash semantics."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select

from safe_agent.contracts.inference_coordinator import TrustedDispatchAuthenticator
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.inference_admission import (
    InferenceAdmissionDenied,
    TrustedInferenceAdmission,
)
from safe_agent.storage.inference_dispatch import (
    InferenceDispatchDenied,
    TrustedInferenceDispatchStore,
)
from safe_agent.storage.models import InferenceBudgetAccountRow, InferenceInvocationRow
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy


@pytest.fixture
def repo(tmp_path):
    repository = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'dispatch.sqlite'}")
    yield repository
    repository.close()


def configured(repo):
    ticks = [NOW]
    admission = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: ticks[0])
    args = assigned(repo)
    admission.admit(*args)
    store = TrustedInferenceDispatchStore(admission)
    return args, ticks, admission, store


def held(repo):
    with repo._sessions() as db:
        return db.scalars(select(InferenceBudgetAccountRow.held_microusd)).all()


def test_start_and_finish_keep_canonical_cost_holds(repo):
    args, ticks, admission, store = configured(repo)
    claim = store.claim(*args)
    assert claim is not None
    assert "token" not in claim.model_dump_json()
    before = held(repo)
    request, marker = store.start(*args, claim)
    assert marker.startswith("sha256:")
    assert held(repo) == before
    assert admission.admit(*args).reservation.status == "UNCERTAIN"
    view, answer = store.finish(*args, claim, OfflineMockInferenceTransport().infer(request))
    assert view.state == "RESPONSE_VALIDATED"
    assert view.proposal_hash == answer.proposal_digest
    assert answer.provider_effect_performed is False
    assert held(repo) == before
    assert store.claim(*args) is None
    with pytest.raises(InferenceDispatchDenied):
        store.start(*args, claim)
    assert store.recover(*args) == view


def test_stale_claim_is_fenced_after_safe_predispatch_reclaim(repo):
    args, ticks, _, store = configured(repo)
    first = store.claim(*args)
    ticks[0] += timedelta(seconds=5)
    second = store.claim(*args)
    assert second.claim_version == first.claim_version + 1
    with pytest.raises(InferenceDispatchDenied):
        store.start(*args, first)
    store.start(*args, second)
    assert store.claim(*args) is None


@pytest.mark.parametrize("field,value", [("claim_version", 99), ("token", SecretStr("f" * 64))])
def test_forged_claim_cannot_start(repo, field, value):
    args, _, _, store = configured(repo)
    claim = store.claim(*args)
    with pytest.raises(InferenceDispatchDenied):
        store.start(*args, claim.model_copy(update={field: value}))
    assert store.recover(*args).state == "RESERVED"


def test_unknown_never_resends_or_releases_capacity(repo):
    args, ticks, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    before = held(repo)
    ticks[0] += timedelta(seconds=11)
    restarted = TrustedInferenceDispatchStore(TrustedInferenceAdmission(
        repo, policy=policy(), clock=lambda: ticks[0],
    ))
    assert restarted.recover(*args).state == "UNKNOWN"
    assert restarted.claim(*args) is None
    with pytest.raises(InferenceDispatchDenied):
        restarted.start(*args, claim)
    assert held(repo) == before
    with pytest.raises(InferenceAdmissionDenied):
        # Same tenant's quota can also be constrained; expired work still counts.
        limited = TrustedInferenceAdmission(repo, policy=policy(global_outstanding=1,
                tenant_outstanding=1, task_outstanding=1), clock=lambda: ticks[0])
        limited.admit(*assigned(repo, now=ticks[0]))
    assert request.request_id == admission.admit(*args).binding.request_id


def test_expired_reserved_cannot_be_started_by_paused_worker(repo):
    args, ticks, _, store = configured(repo)
    claim = store.claim(*args)
    before = held(repo)
    ticks[0] += timedelta(seconds=11)
    view = store.recover(*args)
    assert view.state == "FAILED_BEFORE_DISPATCH"
    assert held(repo) == before  # Capacity release is NOT a financial refund.
    with pytest.raises(InferenceDispatchDenied):
        store.start(*args, claim)


def test_start_rollback_prevents_marker_and_preserves_claim(repo):
    args, _, _, store = configured(repo)
    claim = store.claim(*args)
    def fail_commit(session):
        raise RuntimeError("PRIVATE_DATABASE_DIAGNOSTIC")
    event.listen(repo._sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(InferenceDispatchDenied, match="^INFERENCE_DISPATCH_DENIED$"):
            store.start(*args, claim)
    finally:
        event.remove(repo._sessions, "before_commit", fail_commit)
    assert store.recover(*args).state == "RESERVED"
    store.start(*args, claim)


def test_concurrent_claims_have_one_owner(repo):
    args, _, admission, _ = configured(repo)
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        try:
            return TrustedInferenceDispatchStore(admission).claim(*args)
        except InferenceDispatchDenied:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: run(), range(2)))
    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.parametrize("change", ["wrong_scope", "oversized", "late", "clock_regression", "bad_usage"])
def test_response_failures_cannot_record_success(repo, change):
    args, ticks, _, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    reply = OfflineMockInferenceTransport().infer(request)
    if change == "wrong_scope":
        reply = reply.model_copy(update={"request_id": "inf_" + "a" * 32})
    elif change == "oversized":
        reply = reply.model_copy(update={"output_json": "x" * 9000})
    elif change == "bad_usage":
        reply = reply.model_copy(update={"reported_usage": reply.reported_usage.model_copy(
            update={"input_tokens": 900000})})
    elif change == "late":
        ticks[0] += timedelta(seconds=11)
    else:
        ticks[0] -= timedelta(seconds=1)
    with pytest.raises(InferenceDispatchDenied):
        store.finish(*args, claim, reply)
    with repo._sessions() as db:
        assert db.get(InferenceInvocationRow, request.request_id).state == "DISPATCH_STARTED"


def transport():
    return CoordinatedUDSInferenceTransport(Path("/nonexistent/private.sock"),
        expected_host="inference.example.com", workload_token=SecretStr("w" * 64),
        dispatch_key=SecretStr("d" * 64), clock=lambda: NOW)


@pytest.mark.parametrize("outcome", ["success", "lost_response", "rejected_reply", "process_crash"])
def test_coordinator_calls_backend_at_most_once(repo, monkeypatch, outcome):
    args, ticks, admission, _ = configured(repo)
    wire = transport()
    calls = []
    class Crash(BaseException):
        pass
    def send(req, claim, marker):
        calls.append(req.request_id)
        if outcome == "lost_response":
            raise OSError("SECRET_RESPONSE_LOST")
        if outcome == "process_crash":
            raise Crash()
        reply = OfflineMockInferenceTransport().infer(req)
        if outcome == "rejected_reply":
            return reply.model_copy(update={"output_json": "{\"SECRET\":true}"})
        return reply
    monkeypatch.setattr(wire, "send", send)
    coordinator = TrustedInferenceCoordinator(admission, wire)
    if outcome == "process_crash":
        with pytest.raises(Crash):
            coordinator.execute(*args)
    else:
        receipt = coordinator.execute(*args)
        assert receipt.invocation.state == ("RESPONSE_VALIDATED" if outcome == "success" else "UNKNOWN")
    ticks[0] += timedelta(seconds=11)
    restarted = TrustedInferenceCoordinator(admission, wire)
    again = restarted.execute(*args)
    assert not again.dispatched_this_call
    assert len(calls) == 1
    assert held(repo) == [205, 205, 205]


def test_envelope_authentication_is_bound_to_exact_request(repo):
    args, _, _, store = configured(repo)
    claim = store.claim(*args)
    request, marker = store.start(*args, claim)
    signer = TrustedDispatchAuthenticator(SecretStr("k" * 64))
    envelope = signer.seal(request, claim, marker)
    assert signer.verify(envelope) == request
    for changed in ({"claim_version": 9}, {"binding_hash": "sha256:" + "a" * 64},
                    {"dispatch_event_hash": "sha256:" + "a" * 64}, {"signature": "f" * 64}):
        with pytest.raises(ValueError):
            signer.verify(envelope.model_copy(update=changed))
    with pytest.raises(ValueError):
        TrustedDispatchAuthenticator(SecretStr("z" * 64)).verify(envelope)


def test_missing_admission_or_database_never_calls_transport(repo, monkeypatch):
    args = assigned(repo)
    store = TrustedInferenceDispatchStore(TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW))
    with pytest.raises(InferenceDispatchDenied):
        store.claim(*args)
    wire = transport()
    calls = []
    monkeypatch.setattr(wire, "send", lambda *args: calls.append(args))
    def unavailable(*args, **kwargs):
        raise OSError("DATABASE_SECRET")
    monkeypatch.setattr(repo, "write_task", unavailable)
    with pytest.raises(InferenceDispatchDenied, match="^INFERENCE_DISPATCH_DENIED$"):
        TrustedInferenceCoordinator(store.admission, wire).execute(*args)
    assert calls == []


def test_unknown_consumes_same_policy_capacity_after_expiry(repo):
    ticks = [NOW]
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    admission = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0])
    store = TrustedInferenceDispatchStore(admission)
    args = assigned(repo)
    admission.admit(*args)
    claim = store.claim(*args)
    store.start(*args, claim)
    ticks[0] += timedelta(seconds=11)
    assert store.recover(*args).state == "UNKNOWN"
    with pytest.raises(InferenceAdmissionDenied):
        admission.admit(*assigned(repo, now=ticks[0], org="second_org"))
    assert held(repo) == [205, 205, 205]


def test_coordinator_schema_export_matches_pinned_bytes():
    from scripts.export_m7_coordinator_schemas import TARGET, export
    assert TARGET.read_bytes() == export()
