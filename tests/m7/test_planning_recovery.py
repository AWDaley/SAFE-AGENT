"""API recovery acceptance against W4 services, not a replacement inference path."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from safe_agent.api import create_app
from safe_agent.contracts.planning import PlanningCommand
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.storage.coordinated_models import CoordinatedPlanningConsumptionRow
from safe_agent.storage.models import (
    InferenceInvocationRow,
    OutboxRow,
    PlanningAttemptRow,
    ReferenceRow,
)
from tests.m6.test_persistent_planning import COMMAND, plan
from tests.m6.test_synthetic_workflow import new_task
from tests.m6.test_task_intake import auth
from tests.m7.test_coordinated_planning import build, count, input_for, reopened
from tests.m7.test_inference_coordinator import held


def read(client, signer, actor, task):
    return client.get(f"/v1/tasks/{task}/planning/recovery", headers=auth(signer, actor))


def command(data):
    return {"schema_version": "safe_agent.resume_saved_result.v1",
            "expected_state_version": data["task_state_version"],
            "expected_workspace_version": data["workspace_version"],
            "expected_recovery_revision": data["recovery_revision"]}


def resume(client, signer, actor, task, body):
    return client.post(f"/v1/tasks/{task}/planning/resume-saved",
                       headers=auth(signer, actor), json=body)


def saved(kit, monkeypatch):
    rig = build(kit, monkeypatch)
    client, signer, actor, _, _, service, _, _ = rig
    task = new_task(client, signer, actor)
    with monkeypatch.context() as scoped:
        def stop(*_):
            raise RuntimeError("synthetic stop before preparation")
        scoped.setattr(service, "_consume", stop)
        assert plan(client, signer, actor, task).status_code == 409
    return rig, task


def forbid_inference(monkeypatch, service):
    def forbidden(*_):
        raise AssertionError("recovery must not call inference")
    monkeypatch.setattr(service._inference, "execute", forbidden)
    monkeypatch.setattr(service._inference._transport, "send", forbidden)
    monkeypatch.setattr(service._inference.store.admission, "admit", forbidden)


def test_status_does_not_create_attempt_or_admission(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    forbid_inference(monkeypatch, service)
    response = read(client, signer, actor, task)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "NOT_STARTED"
    assert response.json()["allowed_actions"] == ["refresh", "retire_task"]
    assert resume(client, signer, actor, task, command(response.json())).status_code == 409
    assert count(kit["repo"], PlanningAttemptRow) == count(kit["repo"], InferenceInvocationRow) == 0
    assert calls == []


def test_recoverable_reads_have_no_sql_mutations_or_private_payload(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, service, calls, ticks = rig
    before = held(kit["repo"])
    forbid_inference(monkeypatch, service)
    writes = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(statement.split()[0])
    event.listen(kit["repo"].engine, "before_cursor_execute", observe)
    try:
        first = read(client, signer, actor, task)
        ticks[0] += timedelta(seconds=1)
        second = read(client, signer, actor, task)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["status"] == "RESULT_RECOVERABLE"
        assert first.json()["allowed_actions"] == [
            "refresh", "resume_saved_result", "retire_task",
        ]
    finally:
        event.remove(kit["repo"].engine, "before_cursor_execute", observe)
    assert writes == []
    assert held(kit["repo"]) == before and len(calls) == 1
    for private in ("ref_", "cal_main_private", "cust_c1", "source_fingerprint", "output_json", "prompt_digest"):
        assert private not in first.text


def test_resume_uses_saved_result_without_inference_approval_or_outbox(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, calendar, internal, service, calls, _ = rig
    forbid_inference(monkeypatch, service)
    before = held(kit["repo"])
    state = read(client, signer, actor, task).json()
    result = resume(client, signer, actor, task, command(state))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "PREPARED"
    assert result.json()["allowed_actions"] == ["refresh", "retire_task"]
    assert kit["repo"].replay_task(task).phase is OfficePhase.AWAITING_APPROVAL
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == 1
    assert count(kit["repo"], PlanningAttemptRow) == 1
    assert count(kit["repo"], OutboxRow) == calendar.effect_count == internal.effect_count == 0
    assert len(calls) == 1 and held(kit["repo"]) == before
    # An old command is refused, not silently interpreted as another action.
    assert resume(client, signer, actor, task, command(state)).status_code == 409
    assert read(client, signer, actor, task).json() == result.json()


def test_unknown_remains_uncertain_after_deadline_and_never_refunds(kit, monkeypatch):
    def timeout(_):
        raise TimeoutError("synthetic response loss")
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch, reply=timeout)
    task = new_task(client, signer, actor)
    assert plan(client, signer, actor, task).status_code == 409
    before = held(kit["repo"])
    forbid_inference(monkeypatch, service)
    ticks[0] += timedelta(minutes=2)
    result = read(client, signer, actor, task)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "INFERENCE_UNCERTAIN"
    assert resume(client, signer, actor, task, command(result.json())).status_code == 409
    assert held(kit["repo"]) == before and len(calls) == 1
    assert count(kit["repo"], PlanningAttemptRow) == 1
    assert count(kit["repo"], OutboxRow) == 0


def test_started_status_is_observational_and_does_not_rewrite_history(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    value = service._input(actor, task, PlanningCommand.model_validate(COMMAND))
    store = service._inference.store
    args = (actor, value.lease, value.prompt())
    store.admission.admit(*args)
    assert read(client, signer, actor, task).json()["status"] == "WAITING_TO_DISPATCH"
    claim = store.claim(*args)
    assert claim is not None
    store.start(*args, claim)
    assert read(client, signer, actor, task).json()["status"] == "INFERENCE_PENDING"
    ticks[0] += timedelta(seconds=11)
    data = read(client, signer, actor, task).json()
    assert data["status"] == "INFERENCE_UNCERTAIN"
    with kit["repo"]._sessions() as db:
        row = db.scalars(select(InferenceInvocationRow)).one()
        assert row.state == "DISPATCH_STARTED"
    assert calls == []


def test_expired_result_is_not_resumable(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, service, calls, ticks = rig
    original = read(client, signer, actor, task).json()
    ticks[0] += timedelta(seconds=11)
    forbid_inference(monkeypatch, service)
    data = read(client, signer, actor, task).json()
    assert data["status"] == "RESULT_EXPIRED"
    assert data["allowed_actions"] == ["refresh", "retire_task"]
    assert resume(client, signer, actor, task, command(original)).status_code == 409
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == 0 and len(calls) == 1


def test_revocation_between_inspection_and_resume_is_rechecked(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, service, _, _ = rig
    original = read(client, signer, actor, task).json()
    with kit["repo"]._sessions.begin() as db:
        row = db.scalars(select(ReferenceRow).where(ReferenceRow.task_id == task)).first()
        row.revoked = True
    forbid_inference(monkeypatch, service)
    data = read(client, signer, actor, task).json()
    assert data["status"] == "AUTHORITY_CHANGED"
    assert resume(client, signer, actor, task, command(original)).status_code == 409
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == 0


def test_same_tenant_reader_cannot_resume_and_foreign_task_is_hidden(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, _, _, _ = rig
    data = read(client, signer, kit["manager"], task)
    assert data.status_code == 200, data.text
    assert data.json()["allowed_actions"] == ["refresh"]
    assert resume(client, signer, kit["manager"], task, command(data.json())).status_code == 404
    foreign = actor.model_copy(update={"organization_id": "org_foreign"})
    assert read(client, signer, foreign, task).status_code == 404
    assert read(client, signer, foreign, "task_missing").status_code == 404
    assert client.get(f"/v1/tasks/{task}/planning/recovery").status_code == 401


def test_closed_resume_command_and_state_revision_are_enforced(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, _, _, _ = rig
    data = read(client, signer, actor, task).json()
    body = command(data)
    assert resume(client, signer, actor, task, {**body, "approve": True}).status_code == 422
    assert resume(client, signer, actor, task, {**body, "expected_state_version": True}).status_code == 422
    assert resume(client, signer, actor, task, {**body, "expected_recovery_revision": "sha256:" + "0" * 64}).status_code == 409
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == 0


def test_lost_commit_ack_reads_existing_consumption(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, service, calls, _ = rig
    forbid_inference(monkeypatch, service)
    data = read(client, signer, actor, task).json()
    armed = [False]
    def arm(*_):
        armed[0] = True
    def lose(_):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("synthetic acknowledgment loss")
    event.listen(CoordinatedPlanningConsumptionRow, "before_insert", arm)
    event.listen(kit["repo"]._sessions, "after_commit", lose)
    try:
        result = resume(client, signer, actor, task, command(data))
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "PREPARED"
    finally:
        event.remove(CoordinatedPlanningConsumptionRow, "before_insert", arm)
        event.remove(kit["repo"]._sessions, "after_commit", lose)
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == len(calls) == 1


def test_simultaneous_resume_commands_consume_once(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, service, calls, _ = rig
    forbid_inference(monkeypatch, service)
    body = command(read(client, signer, actor, task).json())
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: resume(client, signer, actor, task, body).status_code, range(2)))
    assert 200 in statuses and set(statuses) <= {200, 409}
    assert count(kit["repo"], CoordinatedPlanningConsumptionRow) == len(calls) == 1
    assert count(kit["repo"], OutboxRow) == 0


def test_saved_result_recovers_with_fresh_services_and_forbidden_inference(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    _, _, _, _, _, _, calls, ticks = rig
    original = input_for(kit["repo"], task)
    fresh = reopened(kit)
    try:
        client, signer, actor, _, _, service, _, _ = build(fresh, monkeypatch, calls=calls, ticks=ticks)
        forbid_inference(monkeypatch, service)
        state = read(client, signer, actor, task).json()
        assert resume(client, signer, actor, task, command(state)).status_code == 200
        assert input_for(fresh["repo"], task) == original and len(calls) == 1
    finally:
        fresh["repo"].close()


def test_recovery_responses_have_no_store_headers(kit, monkeypatch):
    rig, task = saved(kit, monkeypatch)
    client, signer, actor, _, _, _, _, _ = rig
    response = read(client, signer, actor, task)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_input_saved_without_admission_cannot_be_dispatched_by_resume(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    service._input(actor, task, PlanningCommand.model_validate(COMMAND))
    forbid_inference(monkeypatch, service)
    response = read(client, signer, actor, task)
    assert response.status_code == 200 and response.json()["status"] == "INPUT_SAVED"
    assert resume(client, signer, actor, task, command(response.json())).status_code == 409
    assert count(kit["repo"], InferenceInvocationRow) == 0 and calls == []


def test_recovery_assets_are_opt_in_fixed_and_not_cached(kit, monkeypatch):
    client, signer, _, _, _, service, _, _ = build(kit, monkeypatch)
    assert client.get("/operator/recovery").status_code == 404
    enabled = TestClient(create_app(
        kit["coordinator"], signer, clock=service._now,
        synthetic_workflow=service._workflow, persistent_planning=service,
        enable_operator_ui=True,
    ))
    for path in ("/operator/recovery", "/operator/assets/recovery.js"):
        response = enabled.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in response.headers["content-security-policy"]
    assert enabled.get("/operator/assets/unknown.txt").status_code == 404
