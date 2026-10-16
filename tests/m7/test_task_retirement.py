"""Terminal coordinated task retirement without retry, refund, or effect dispatch."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.contracts.planning import PlanningCommand
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.storage.inference_dispatch import InferenceDispatchDenied
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    InferenceInvocationRow,
    OutboxRow,
)
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import COMMAND, plan
from tests.m6.test_synthetic_workflow import grant, new_task, queue
from tests.m6.test_task_intake import auth
from tests.m7.test_coordinated_planning import build, count
from tests.m7.test_inference_coordinator import held
from tests.m7.test_planning_recovery import read, saved


def retire_command(data, reason="NO_LONGER_NEEDED"):
    return {
        "schema_version": "safe_agent.retire_task.v1",
        "expected_state_version": data["task_state_version"],
        "expected_workspace_version": data["workspace_version"],
        "expected_recovery_revision": data["recovery_revision"],
        "reason_code": reason,
    }


def retire(client, signer, actor, task, data, reason="NO_LONGER_NEEDED"):
    return client.post(
        f"/v1/tasks/{task}/planning/retire",
        headers=auth(signer, actor),
        json=retire_command(data, reason),
    )


def invocation(repo):
    with repo._sessions() as db:
        return db.scalars(select(InferenceInvocationRow)).one()


def reservation(repo):
    with repo._sessions() as db:
        return db.scalars(select(InferenceBudgetReservationRow)).one()


def test_not_started_task_retires_terminally_and_new_task_is_separate(kit, monkeypatch):
    client, signer, actor, _, _, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    before = read(client, signer, actor, task)
    assert before.status_code == 200
    assert before.json()["status"] == "NOT_STARTED"
    assert before.json()["allowed_actions"] == ["refresh", "retire_task"]

    result = retire(client, signer, actor, task, before.json(), "REPLACE_WITH_NEW_TASK")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "TASK_RETIRED"
    assert result.json()["task_phase"] == "RETIRED"
    assert result.json()["allowed_actions"] == ["refresh"]
    assert kit["repo"].replay_task(task).phase is OfficePhase.RETIRED
    assert client.get(f"/v1/tasks/{task}/planning", headers=auth(signer, actor)).json()[
        "status"
    ] == "RETIRED"
    assert plan(client, signer, actor, task).status_code == 409
    assert count(kit["repo"], InferenceInvocationRow) == count(kit["repo"], OutboxRow) == 0
    assert calls == []

    events = kit["repo"].read_events(task)
    assert events[-1].kind is EventKind.TASK_RETIRED
    assert events[-1].payload == {"reason_code": "REPLACE_WITH_NEW_TASK"}

    replacement = new_task(
        client,
        signer,
        actor,
        params={"idempotency_key": "req_" + "b" * 32},
    )
    assert replacement != task
    assert kit["repo"].replay_task(replacement).phase is OfficePhase.ASSIGNED


def test_reserved_invocation_is_cancelled_before_dispatch_but_hold_is_preserved(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    value = service._input(actor, task, PlanningCommand.model_validate(COMMAND))
    store = service._inference.store
    args = (actor, value.lease, value.prompt())
    store.admission.admit(*args)
    claim = store.claim(*args)
    assert claim is not None
    before_hold = held(kit["repo"])

    state = read(client, signer, actor, task).json()
    assert state["status"] == "WAITING_TO_DISPATCH"
    result = retire(client, signer, actor, task, state, "STALE_OR_BLOCKED")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "TASK_RETIRED"
    row = invocation(kit["repo"])
    assert row.state == "FAILED_BEFORE_DISPATCH"
    assert row.reason_code == "CANCELLED_BEFORE_DISPATCH"
    assert reservation(kit["repo"]).status == "UNCERTAIN"
    assert held(kit["repo"]) == before_hold
    with pytest.raises(InferenceDispatchDenied):
        store.start(*args, claim)
    assert count(kit["repo"], OutboxRow) == 0 and calls == []


def test_started_inference_cannot_retire_before_deadline(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    value = service._input(actor, task, PlanningCommand.model_validate(COMMAND))
    store = service._inference.store
    args = (actor, value.lease, value.prompt())
    store.admission.admit(*args)
    claim = store.claim(*args)
    assert claim is not None
    store.start(*args, claim)

    state = read(client, signer, actor, task).json()
    assert state["status"] == "INFERENCE_PENDING"
    assert state["allowed_actions"] == ["refresh"]
    assert retire(client, signer, actor, task, state).status_code == 409
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED
    assert invocation(kit["repo"]).state == "DISPATCH_STARTED"
    assert calls == []


def test_expired_started_inference_becomes_unknown_then_task_retires_without_refund(
    kit, monkeypatch,
):
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    value = service._input(actor, task, PlanningCommand.model_validate(COMMAND))
    store = service._inference.store
    args = (actor, value.lease, value.prompt())
    store.admission.admit(*args)
    claim = store.claim(*args)
    assert claim is not None
    store.start(*args, claim)
    before_hold = held(kit["repo"])
    ticks[0] += timedelta(seconds=11)

    state = read(client, signer, actor, task).json()
    assert state["status"] == "INFERENCE_UNCERTAIN"
    assert state["allowed_actions"] == ["refresh", "retire_task"]
    result = retire(client, signer, actor, task, state, "STALE_OR_BLOCKED")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "TASK_RETIRED"
    assert invocation(kit["repo"]).state == "UNKNOWN"
    assert held(kit["repo"]) == before_hold
    assert count(kit["repo"], OutboxRow) == 0 and calls == []


def test_saved_result_can_be_retired_instead_of_consumed(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, _, calls, _ = rig
    before_hold = held(kit["repo"])
    state = read(client, signer, actor, task).json()
    assert state["status"] == "RESULT_RECOVERABLE"
    assert state["allowed_actions"] == ["refresh", "resume_saved_result", "retire_task"]

    result = retire(client, signer, actor, task, state, "NO_LONGER_NEEDED")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "TASK_RETIRED"
    assert count(kit["repo"], OutboxRow) == 0
    assert len(calls) == 1 and held(kit["repo"]) == before_hold


def test_prepared_task_can_retire_before_approval_or_queue(kit, monkeypatch):
    client, signer, actor, calendar, internal, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    prepared = plan(client, signer, actor, task)
    assert prepared.status_code == 200
    prepared_state = kit["repo"].replay_task(task)
    assert prepared_state.phase is OfficePhase.AWAITING_APPROVAL
    state = read(client, signer, actor, task).json()
    assert state["status"] == "PREPARED"
    result = retire(client, signer, actor, task, state, "NO_LONGER_NEEDED")
    assert result.status_code == 200, result.text
    assert kit["repo"].replay_task(task).phase is OfficePhase.RETIRED
    assert count(kit["repo"], OutboxRow) == 0
    assert calendar.effect_count == internal.effect_count == 0
    assert len(calls) == 1

    approval = client.post(
        f"/v1/tasks/{task}/approvals",
        headers=auth(signer, kit["manager"]),
        json={
            "proposal_hash": prepared.json()["proposal_hash"],
            "expected_state_version": prepared_state.state_version,
            "approved": True,
            "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
    )
    assert approval.status_code == 409


def test_same_tenant_reader_and_stale_commands_cannot_retire(kit, monkeypatch):
    client, signer, actor, _, _, _, _, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    state = read(client, signer, actor, task).json()
    manager = read(client, signer, kit["manager"], task)
    assert manager.status_code == 200 and manager.json()["allowed_actions"] == ["refresh"]
    assert retire(client, signer, kit["manager"], task, state).status_code == 404

    stale = retire_command(state)
    stale["expected_recovery_revision"] = "sha256:" + "0" * 64
    assert client.post(
        f"/v1/tasks/{task}/planning/retire",
        headers=auth(signer, actor),
        json=stale,
    ).status_code == 409
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED


@pytest.mark.parametrize("reason", ["", "UNKNOWN", "cancel please", 1, True])
def test_retirement_reason_is_closed_schema(kit, monkeypatch, reason):
    client, signer, actor, _, _, _, _, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    state = read(client, signer, actor, task).json()
    body = retire_command(state)
    body["reason_code"] = reason
    assert client.post(
        f"/v1/tasks/{task}/planning/retire",
        headers=auth(signer, actor),
        json=body,
    ).status_code == 422
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED


def test_queued_execution_intent_cannot_be_retired(kit, monkeypatch):
    client, signer, actor, calendar, internal, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    prepared = plan(client, signer, actor, task)
    assert prepared.status_code == 200, prepared.text
    version = grant(client, signer, kit["manager"], task, prepared.json())
    queued = queue(client, signer, actor, task, version=version)
    assert queued.status_code == 200, queued.text
    assert kit["repo"].replay_task(task).phase is OfficePhase.EXECUTING
    assert count(kit["repo"], OutboxRow) == 1
    assert calendar.effect_count == internal.effect_count == 0

    status = read(client, signer, actor, task)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "TASK_ADVANCED"
    assert status.json()["allowed_actions"] == ["refresh"]
    assert retire(client, signer, actor, task, status.json()).status_code == 409
    assert kit["repo"].replay_task(task).phase is OfficePhase.EXECUTING
    assert count(kit["repo"], OutboxRow) == 1
    assert len(calls) == 1
