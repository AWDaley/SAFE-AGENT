"""W4 operator integration, restart, immutable consumption and approval boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, insert, select, update
from sqlalchemy.exc import DBAPIError

from safe_agent.api import create_app
from safe_agent.broker import ReferenceBroker
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import RequestClarification
from safe_agent.contracts.planning import PlanningCommand
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.orchestration.coordinated_planning import create_coordinated_planning
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.storage.coordinated_models import CoordinatedPlanningConsumptionRow as ConsumptionRow
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow as InputRow
from safe_agent.storage.coordinated_planning import read_input
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    InferenceInvocationRow,
    OutboxRow,
    PlanningAttemptRow,
    PlanningOutcomeRow,
    ReferenceRow,
)
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.storage.result_models import InferenceValidatedResultRow as ResultRow
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import VirtualOffice
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import COMMAND, plan, setup, view
from tests.m6.test_synthetic_workflow import grant, new_task, queue
from tests.m6.test_task_intake import auth, provisioner
from tests.m7.admission_fixtures import policy
from tests.m7.test_inference_coordinator import held, transport


def build(kit, monkeypatch, *, calls=None, ticks=None, reply=None):
    calls = [] if calls is None else calls
    ticks = [NOW] if ticks is None else ticks
    _, signer, employee, calendar, internal, old, store = setup(kit, clock=lambda: ticks[0])
    wire = transport()
    def send(request, claim, marker):
        calls.append(request.request_id)
        return reply(request) if reply else OfflineMockInferenceTransport().infer(request)
    monkeypatch.setattr(wire, "send", send)
    service = create_coordinated_planning(store, old._workflow,
        admission=TrustedInferenceAdmission(kit["repo"], policy=policy(), clock=lambda: ticks[0]),
        transport=wire)
    client = TestClient(create_app(kit["coordinator"], signer, clock=lambda: ticks[0],
        task_provisioner=provisioner(kit), synthetic_workflow=old._workflow,
        persistent_planning=service))
    return client, signer, employee, calendar, internal, service, calls, ticks


def count(repo, model):
    with repo._sessions() as db:
        return db.scalar(select(func.count()).select_from(model))


def input_for(repo, task_id):
    with repo._sessions() as db:
        return read_input(db.scalar(select(InputRow).where(InputRow.task_id == task_id)))


def reopened(kit):
    fresh = LedgerRepository(create_engine(kit["repo"].engine.url))
    broker = ReferenceBroker(secret=b"m2_test_private_broker_key_never_prod_000001")
    office = VirtualOffice(kit["office"]._source, broker)
    copy = {**kit, "repo": fresh, "broker": broker, "office": office,
            "coordinator": TrustedOfficeTasks(fresh, office, OfficeGate(broker))}
    return copy


def test_operator_prepares_exact_final_action_then_requires_separate_approval(kit, monkeypatch):
    client, signer, actor, calendar, internal, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    response = plan(client, signer, actor, task)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PREPARED"
    assert response.json()["attempts_used"] == 1
    assert count(kit["repo"], OutboxRow) == 0
    assert calendar.effect_count == internal.effect_count == 0
    with kit["repo"]._sessions() as db:
        used = db.scalars(select(ConsumptionRow)).one()
        result = db.scalars(select(ResultRow)).one()
        assert used.candidate_hash == result.candidate_hash
        assert used.proposal_hash == response.json()["proposal_hash"]
        assert used.proposal_hash != used.candidate_hash
    snapshot = kit["repo"].replay_task(task)
    assert snapshot.phase is OfficePhase.AWAITING_APPROVAL
    assert queue(client, signer, actor, task, version=snapshot.state_version).status_code == 409
    wrong = client.post(f"/v1/tasks/{task}/approvals", headers=auth(signer, kit["manager"]), json={
        "proposal_hash": result.candidate_hash, "expected_state_version": snapshot.state_version,
        "approved": True, "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    })
    assert wrong.status_code == 409
    version = grant(client, signer, kit["manager"], task, response.json())
    assert queue(client, signer, actor, task, version=version).status_code == 200
    assert calendar.effect_count == 0
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], calendar,
                                     task_adapter=internal, office=kit["office"])
    outcome = worker.process_one(task_id=task, now=NOW)
    assert outcome is not None and outcome.outcome == "COMMITTED"
    assert worker.process_one(task_id=task, now=NOW) is None
    assert calendar.effect_count == 1 and len(calls) == 1
    assert kit["repo"].replay_task(task).phase is OfficePhase.COMPLETED
    for private in ("cal_main_private", "cust_c1", "record_json", "source_fingerprint", "prompt_digest"):
        assert private not in response.text


def test_restart_after_validated_result_reuses_input_and_never_sends_again(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    def crash(*_):
        raise RuntimeError("synthetic stop before consumption")
    monkeypatch.setattr(service, "_consume", crash)
    assert plan(client, signer, actor, task).status_code == 409
    saved = input_for(kit["repo"], task)
    before = held(kit["repo"])
    assert count(kit["repo"], ResultRow) == 1
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED
    fresh = reopened(kit)
    try:
        new, sign, employee, _, _, _, _, _ = build(fresh, monkeypatch, calls=calls, ticks=ticks)
        response = plan(new, sign, employee, task)
        assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert input_for(fresh["repo"], task) == saved
        assert held(fresh["repo"]) == before
        assert count(fresh["repo"], PlanningAttemptRow) == count(fresh["repo"], ConsumptionRow) == 1
        assert plan(new, sign, employee, task).json() == response.json()
        assert count(fresh["repo"], OutboxRow) == 0
    finally:
        fresh["repo"].close()


def test_restart_after_input_before_admission_keeps_same_attempt(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    def stop(*_):
        raise RuntimeError("synthetic stop before admission")
    monkeypatch.setattr(service._inference, "execute", stop)
    assert plan(client, signer, actor, task).status_code == 409
    saved = input_for(kit["repo"], task)
    assert count(kit["repo"], InferenceInvocationRow) == 0
    fresh = reopened(kit)
    try:
        new, sign, employee, _, _, _, _, _ = build(fresh, monkeypatch, calls=calls, ticks=ticks)
        assert plan(new, sign, employee, task).status_code == 200
        assert input_for(fresh["repo"], task) == saved
        assert len(calls) == count(fresh["repo"], PlanningAttemptRow) == 1
    finally:
        fresh["repo"].close()


def test_uncertain_call_cannot_be_replaced_after_lease_expiry_or_generic_failure(kit, monkeypatch):
    def timeout(_):
        raise TimeoutError("synthetic unknown provider outcome")
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch, reply=timeout)
    task = new_task(client, signer, actor)
    assert plan(client, signer, actor, task).status_code == 409
    saved = input_for(kit["repo"], task)
    before = held(kit["repo"])
    ticks[0] += timedelta(minutes=2)
    assert plan(client, signer, actor, task, ws=1).status_code == 409
    with pytest.raises(ValueError):
        service._store.begin(actor, task, expected_task_version=0, expected_workspace_version=1,
                             mode="text_only", now=ticks[0])
    with pytest.raises(ValueError):
        service._store.fail(saved.lease, result_kind="RETRYABLE_FAILURE", reason_code="STALE_OR_DENIED", now=ticks[0])
    assert len(calls) == 1 and held(kit["repo"]) == before
    assert count(kit["repo"], PlanningAttemptRow) == count(kit["repo"], InferenceBudgetReservationRow) == 1
    assert count(kit["repo"], PlanningOutcomeRow) == count(kit["repo"], ConsumptionRow) == 0
    assert view(client, signer, actor, task).json()["status"] == "RUNNING"


def test_consumption_insert_failure_rolls_back_preview_proposal_and_outcome(kit, monkeypatch):
    client, signer, actor, _, _, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    def fail(*_):
        raise RuntimeError("synthetic consumption insert failure")
    event.listen(ConsumptionRow, "before_insert", fail)
    try:
        assert plan(client, signer, actor, task).status_code == 409
    finally:
        event.remove(ConsumptionRow, "before_insert", fail)
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task)) == 1
    assert count(kit["repo"], ConsumptionRow) == count(kit["repo"], PlanningOutcomeRow) == 0
    assert count(kit["repo"], ResultRow) == 1
    assert plan(client, signer, actor, task).status_code == 200
    assert len(calls) == count(kit["repo"], ConsumptionRow) == 1


def test_lost_consumption_commit_ack_recovers_committed_preparation(kit, monkeypatch):
    client, signer, actor, _, _, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    armed = [False]
    def arm(*_):
        armed[0] = True
    def lose(_):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("synthetic lost consumption commit acknowledgment")
    event.listen(ConsumptionRow, "before_insert", arm)
    event.listen(kit["repo"]._sessions, "after_commit", lose)
    try:
        result = plan(client, signer, actor, task)
        assert result.status_code == 200, result.text
        assert plan(client, signer, actor, task).json() == result.json()
    finally:
        event.remove(ConsumptionRow, "before_insert", arm)
        event.remove(kit["repo"]._sessions, "after_commit", lose)
    assert len(calls) == count(kit["repo"], ConsumptionRow) == 1


def test_expired_validated_result_cannot_prepare_or_mint_replacement(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    original = service._consume
    def slow(*args):
        ticks[0] += timedelta(seconds=11)
        return original(*args)
    monkeypatch.setattr(service, "_consume", slow)
    assert plan(client, signer, actor, task).status_code == 409
    monkeypatch.setattr(service, "_consume", original)
    assert plan(client, signer, actor, task, ws=1).status_code == 409
    assert len(calls) == 1 and count(kit["repo"], ResultRow) == 1
    assert count(kit["repo"], ConsumptionRow) == 0
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED


def test_revoked_reference_at_consumption_prevents_proposal(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    original = service._consume
    def revoke(*args):
        with kit["repo"].engine.begin() as db:
            db.execute(update(ReferenceRow).where(ReferenceRow.task_id == task).values(revoked=True))
        return original(*args)
    monkeypatch.setattr(service, "_consume", revoke)
    assert plan(client, signer, actor, task).status_code == 409
    assert len(calls) == 1 and count(kit["repo"], ConsumptionRow) == 0
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED


def test_same_revision_changed_source_content_is_rejected(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    original = service._consume
    def changed(*args):
        reader = service._workflow._gateway._reader
        snapshot = reader.snapshot
        monkeypatch.setattr(reader, "snapshot", lambda **kw: snapshot(**kw).model_copy(
            update={"source_hash": "sha256:" + "0" * 64}))
        return original(*args)
    monkeypatch.setattr(service, "_consume", changed)
    assert plan(client, signer, actor, task).status_code == 409
    assert len(calls) == 1 and count(kit["repo"], ConsumptionRow) == 0
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED


def test_parallel_operator_requests_have_one_inference_and_consumption(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    barrier = Barrier(2)
    def run(_):
        barrier.wait(timeout=10)
        try:
            return service.plan(actor, task, PlanningCommand.model_validate(COMMAND), now=NOW)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    response = plan(client, signer, actor, task)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    for model in (InputRow, ConsumptionRow, PlanningAttemptRow, InferenceInvocationRow, ResultRow):
        assert count(kit["repo"], model) == 1
    assert count(kit["repo"], OutboxRow) == 0


def test_input_and_consumption_are_database_immutable(kit, monkeypatch):
    client, signer, actor, _, _, _, _, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    assert plan(client, signer, actor, task).status_code == 200
    for model in (InputRow, ConsumptionRow):
        with kit["repo"].engine.connect() as db:
            values = dict(db.execute(select(model.__table__)).mappings().one())
        for stmt in (model.__table__.delete(), model.__table__.update().values(task_id="forged"),
                     insert(model).values(**values)):
            with pytest.raises(DBAPIError), kit["repo"].engine.begin() as db:
                db.execute(stmt)


def test_clarification_is_fixed_and_repeat_does_not_start_another_attempt(kit, monkeypatch):
    def clarify(request):
        return OfflineMockInferenceTransport().infer(request).model_copy(update={
            "output_json": RequestClarification(reason_code="INSUFFICIENT_CONTEXT",
                                                 question="PRIVATE_CREDENTIAL_DO_NOT_STORE").model_dump_json()})
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch, reply=clarify)
    task = new_task(client, signer, actor)
    response = plan(client, signer, actor, task)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "NEEDS_CLARIFICATION"
    assert plan(client, signer, actor, task, ws=2).json() == response.json()
    assert len(calls) == 1 and count(kit["repo"], OutboxRow) == 0
    with kit["repo"]._sessions() as db:
        assert "PRIVATE_CREDENTIAL_DO_NOT_STORE" not in db.scalars(select(ResultRow.record_json)).one()
    with pytest.raises(StateConflict):
        service._store.begin(actor, task, expected_task_version=0, expected_workspace_version=2,
                             mode="text_only", now=NOW)


@pytest.mark.parametrize("change", ["tenant", "actor", "scope", "expired", "version", "extra"])
def test_bad_operator_scope_or_command_never_reaches_inference(kit, monkeypatch, change):
    client, signer, actor, _, _, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    changed = actor
    if change == "tenant":
        changed = actor.model_copy(update={"organization_id": "org_other"})
    elif change == "actor":
        changed = actor.model_copy(update={"actor_id": "other_actor"})
    elif change == "scope":
        changed = actor.model_copy(update={"scopes": ("task:read",)})
    elif change == "expired":
        changed = actor.model_copy(update={"authenticated_until": NOW - timedelta(seconds=1)})
    if change == "expired":
        with pytest.raises(ValueError):
            # Exercise the host boundary independently of token issuance rules.
            service = build(kit, monkeypatch, calls=calls)[5]
            service.plan(changed, task, PlanningCommand.model_validate(COMMAND), now=NOW)
    else:
        response = plan(client, signer, changed, task, ws=99 if change == "version" else 0,
                        extra={"mode": "hosted"} if change == "extra" else None)
        assert response.status_code in (401, 403, 404, 409, 422)
    assert calls == []
    assert count(kit["repo"], InferenceInvocationRow) == 0


def test_bootstrap_requires_one_repository_and_current_schema(kit, monkeypatch):
    _, _, _, _, _, service, _, _ = build(kit, monkeypatch)
    other = LedgerRepository.testing_sqlite()
    try:
        with pytest.raises(TypeError):
            create_coordinated_planning(service._store, service._workflow,
                admission=TrustedInferenceAdmission(other, policy=policy(), clock=lambda: NOW), transport=transport())
    finally:
        other.close()
    assert count(kit["repo"], InputRow) == 0
    assert sha256(service._inference.store.admission._policy) == service._inference.store.admission._policy_hash
