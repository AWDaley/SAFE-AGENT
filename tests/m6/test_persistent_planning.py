"""M6-01e: durable, bounded, text-only planning without extra model authority."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.agent.json_planner import ClosedJsonPlanner
from safe_agent.api import create_app
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.demo.m6_planning import run_m6_planning_demo
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.security.principal import SignedOperatorTokens
from safe_agent.storage.models import (
    PlanningAttemptRow, PlanningOutcomeRow, PlanningWorkspaceRow,
)
from safe_agent.storage.planning import PlanningWorkspaceStore
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.tal.office_gate import OfficeGate
from tests.m2.conftest import NOW
from tests.m6.test_synthetic_workflow import grant, new_task, queue
from tests.m6.test_task_intake import SIGNING_KEY, auth, provisioner

ROOT = Path(__file__).resolve().parents[2]
COMMAND = {
    "schema_version": "safe_agent.planning_command.v1",
    "expected_state_version": 0,
    "expected_workspace_version": 0,
}


def setup(kit, *, planner=None, max_attempts=3, clock=None, lease_seconds=90):
    source = kit["office"]._source
    provider = DurableFakeProvider(kit["repo"].engine, source)
    calendar = FakeCalendarAdapter(source, durable_store=provider)
    internal = FakeInternalTaskAdapter(source, durable_store=provider)
    gateway = SyntheticExecutionGateway(
        kit["repo"], kit["office"], OfficeGate(kit["broker"]),
        kit["broker"], calendar, task_adapter=internal,
    )
    workflow = TrustedSyntheticWorkflow(
        kit["repo"], kit["office"], kit["broker"],
        kit["coordinator"], gateway=gateway,
    )
    store = PlanningWorkspaceStore(
        kit["repo"], max_attempts=max_attempts, lease_seconds=lease_seconds,
    )
    persistent = TrustedPersistentPlanning(
        store, workflow, planner=planner, clock=clock or (lambda: NOW),
    )
    signer = SignedOperatorTokens(SIGNING_KEY)
    client = TestClient(create_app(
        kit["coordinator"], signer,
        clock=lambda: NOW, task_provisioner=provisioner(kit),
        synthetic_workflow=workflow, persistent_planning=persistent,
        enable_operator_ui=True,
    ))
    employee = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    return client, signer, employee, calendar, internal, persistent, store


def plan(client, signer, employee, task_id, *, ws=0, state=0, extra=None):
    body = {
        **COMMAND, "expected_state_version": state,
        "expected_workspace_version": ws,
    }
    if extra:
        body.update(extra)
    return client.post(
        "/v1/tasks/" + task_id + "/plan",
        headers=auth(signer, employee), json=body,
    )


def view(client, signer, employee, task_id):
    return client.get(
        "/v1/tasks/" + task_id + "/planning", headers=auth(signer, employee),
    )


def make_proposal_from_prompt(captured):
    def generate(prompt: str) -> str:
        captured.append(prompt)
        payload = json.loads(prompt)
        ctx, projection = payload["task"], payload["availability"]
        from datetime import datetime
        start = datetime.fromisoformat(projection["free_slots"][0]["start_at"])
        end = start + timedelta(minutes=ctx["duration_minutes"])
        return CalendarCreateEvent(
            calendar_ref=ctx["calendar_ref"], customer_ref=ctx["customer_ref"],
            start_at=start, end_at=end, time_zone=ctx["time_zone"],
            template_id=ctx["template_id"], simulation_ref=ctx["simulation_ref"],
            projection_rev=projection["revision"],
        ).model_dump_json()
    return generate


def test_deterministic_workspace_proposal_atomic_and_private(kit):
    client, signer, employee, calendar, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    assert view(client, signer, employee, task_id).json()["workspace_version"] == 0
    response = plan(client, signer, employee, task_id)
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert outcome["status"] == "PREPARED"
    assert outcome["attempts_used"] == 1
    assert outcome["workspace_version"] == 2
    assert outcome["proposal_hash"].startswith("sha256:")
    assert outcome["provider_effect_performed"] is False
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.AWAITING_APPROVAL
    assert calendar.effect_count == 0
    assert view(client, signer, employee, task_id).json()["status"] == "PREPARED"
    with kit["repo"]._sessions() as db:
        workspace = db.get(PlanningWorkspaceRow, task_id)
        assert workspace is not None and workspace.status == "PREPARED"
        assert workspace.proposal_hash == outcome["proposal_hash"]
        attempts = db.scalars(select(PlanningAttemptRow)).all()
        outcomes = db.scalars(select(PlanningOutcomeRow)).all()
        assert len(attempts) == len(outcomes) == 1
        assert outcomes[0].result_kind == "PROPOSED"
        assert outcomes[0].proposal_hash == outcome["proposal_hash"]
    assert plan(client, signer, employee, task_id, ws=2).status_code == 409
    for marker in ("cal_main_private", "cust_c1", "authorization", "email", "token"):
        assert marker not in response.text


def test_closed_text_planner_only_receives_safe_projection_and_never_dispatches(kit):
    prompts = []
    planner = ClosedJsonPlanner(make_proposal_from_prompt(prompts))
    client, signer, employee, calendar, internal, _, _ = setup(kit, planner=planner)
    task_id = new_task(client, signer, employee)
    result = plan(client, signer, employee, task_id)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "PREPARED"
    assert view(client, signer, employee, task_id).json()["mode"] == "text_only"
    assert len(prompts) == 1
    for private in ("cal_main_private", "cust_c1", "customer_name", "oauth",
                    "refresh_token", "password", "@", "sqlalchemy"):
        assert private not in prompts[0]
    assert "free_slots" in prompts[0] and "ref_" in prompts[0]
    assert calendar.effect_count == internal.effect_count == 0
    approved = grant(client, signer, kit["manager"], task_id, result.json())
    assert queue(client, signer, employee, task_id, version=approved).status_code == 200
    assert calendar.effect_count == 0
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal,
        office=kit["office"],
    )
    receipt = worker.process_one(task_id=task_id, now=NOW)
    assert receipt is not None and receipt.outcome == "COMMITTED"
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert calendar.effect_count == 1


def test_model_clarification_is_sanitized_and_replan_uses_same_manifest(kit):
    prompts = []
    def response(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return RequestClarification(
                reason_code="INSUFFICIENT_CONTEXT",
                question="IGNORE RULES - PRIVATE_CUSTOMER_CREDENTIAL",
            ).model_dump_json()
        return make_proposal_from_prompt([])(prompt)

    client, signer, employee, calendar, _, _, _ = setup(
        kit, planner=ClosedJsonPlanner(response),
    )
    task_id = new_task(client, signer, employee)
    original = kit["repo"].read_task_record(task_id)
    first = plan(client, signer, employee, task_id)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "NEEDS_CLARIFICATION"
    assert data["reason_code"] == "INSUFFICIENT_CONTEXT"
    assert data["attempts_used"] == 1
    assert "PRIVATE_CUSTOMER_CREDENTIAL" not in first.text
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task_id)) == 1
    second = plan(client, signer, employee, task_id, ws=data["workspace_version"])
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "PREPARED"
    assert second.json()["attempts_used"] == 2
    assert kit["repo"].read_task_record(task_id) == original
    assert calendar.effect_count == 0
    with kit["repo"]._sessions() as db:
        results = db.scalars(select(PlanningOutcomeRow).order_by(
            PlanningOutcomeRow.recorded_at, PlanningOutcomeRow.attempt_id,
        )).all()
        assert len(results) == 2
        assert all("PRIVATE_CUSTOMER_CREDENTIAL" not in str(x.__dict__) for x in results)


def test_invalid_model_replies_exhaust_budget_without_provider_effect(kit):
    callbacks = []
    def bad_model(prompt):
        callbacks.append(prompt)
        return '{"operation":"calendar.delete_event","admin":true}'

    client, signer, employee, calendar, _, _, _ = setup(
        kit, planner=ClosedJsonPlanner(bad_model), max_attempts=2,
    )
    task_id = new_task(client, signer, employee)
    a = plan(client, signer, employee, task_id)
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "READY"
    assert a.json()["reason_code"] == "MODEL_RESPONSE_REJECTED"
    b = plan(client, signer, employee, task_id, ws=a.json()["workspace_version"])
    assert b.status_code == 200, b.text
    assert b.json()["status"] == "EXHAUSTED"
    assert b.json()["reason_code"] == "ATTEMPT_EXHAUSTED"
    c = plan(client, signer, employee, task_id, ws=b.json()["workspace_version"])
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "EXHAUSTED"
    assert len(callbacks) == 2
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task_id)) == 1
    assert calendar.effect_count == 0


def test_concurrent_claim_and_lease_recovery_preserve_bound(kit):
    client, signer, employee, _, _, _, store = setup(
        kit, max_attempts=2, lease_seconds=10,
    )
    task_id = new_task(client, signer, employee)
    first = store.begin(
        employee, task_id, expected_task_version=0,
        expected_workspace_version=0, mode="deterministic", now=NOW,
    )
    assert first is not None
    assert first.workspace_version == 1
    with pytest.raises(StateConflict):
        store.begin(
            employee, task_id, expected_task_version=0,
            expected_workspace_version=1, mode="deterministic",
            now=NOW + timedelta(seconds=1),
        )
    status = view(client, signer, employee, task_id)
    assert status.json()["status"] == "RUNNING"
    assert status.json()["attempts_used"] == 1
    with pytest.raises(StateConflict):
        store.begin(
            employee, task_id, expected_task_version=0,
            expected_workspace_version=0, mode="deterministic",
            now=NOW + timedelta(seconds=11),
        )
    recovered = store.begin(
        employee, task_id, expected_task_version=0,
        expected_workspace_version=1, mode="deterministic",
        now=NOW + timedelta(seconds=11),
    )
    assert recovered is not None and recovered.attempt_number == 2
    assert recovered.workspace_version == 3
    with pytest.raises(StateConflict):
        store.fail(
            first, result_kind="RETRYABLE_FAILURE",
            reason_code="MODEL_RESPONSE_REJECTED", now=NOW + timedelta(seconds=12),
        )
    store.fail(
        recovered, result_kind="RETRYABLE_FAILURE",
        reason_code="MODEL_RESPONSE_REJECTED", now=NOW + timedelta(seconds=12),
    )
    assert view(client, signer, employee, task_id).json()["status"] == "EXHAUSTED"
    with kit["repo"]._sessions() as db:
        outcomes = db.scalars(select(PlanningOutcomeRow)).all()
        assert {x.result_kind for x in outcomes} == {
            "LEASE_EXPIRED", "RETRYABLE_FAILURE",
        }


def test_model_overruns_lease_without_admitting_a_proposal(kit):
    current = [NOW]
    def slow_model(prompt):
        current[0] += timedelta(minutes=2)
        return make_proposal_from_prompt([])(prompt)

    client, signer, employee, _, _, _, _ = setup(
        kit, planner=ClosedJsonPlanner(slow_model),
        clock=lambda: current[0], lease_seconds=10,
    )
    task_id = new_task(client, signer, employee)
    result = plan(client, signer, employee, task_id)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "READY"
    assert result.json()["reason_code"] == "LEASE_EXPIRED"
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task_id)) == 1


def test_planning_survives_sql_restart_and_keeps_attempt_history(kit, tmp_path):
    client, signer, employee, _, _, _, _ = setup(
        kit, planner=ClosedJsonPlanner(lambda _: RequestClarification(
            reason_code="NO_AVAILABLE_SLOT", question="Retry later",
        ).model_dump_json()),
    )
    task_id = new_task(client, signer, employee)
    before = plan(client, signer, employee, task_id)
    assert before.status_code == 200 and before.json()["status"] == "NEEDS_CLARIFICATION"
    assert len(kit["repo"].read_events(task_id)) == 1
    # Reopen the same file-backed SQL history without resetting attempts.
    fresh_repo = LedgerRepository.testing_sqlite(
        f"sqlite+pysqlite:///{tmp_path / 'm2.sqlite'}",
    )
    try:
        recovered = PlanningWorkspaceStore(fresh_repo, max_attempts=3).view(
            employee, task_id, now=NOW, mode="text_only",
        )
        assert recovered.attempts_used == 1
        assert recovered.workspace_version == before.json()["workspace_version"]
        assert recovered.last_reason == "NO_AVAILABLE_SLOT"
    finally:
        fresh_repo.close()


def test_outcome_tampering_and_mode_change_fail_closed(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    assert plan(client, signer, employee, task_id).status_code == 200
    with kit["repo"]._sessions.begin() as db:
        outcome = db.scalars(select(PlanningOutcomeRow)).one()
        db.execute(update(PlanningOutcomeRow).where(
            PlanningOutcomeRow.attempt_id == outcome.attempt_id,
        ).values(reason_code="TAMPERED_SECRET"))
    assert view(client, signer, employee, task_id).status_code == 409
    with pytest.raises(StateConflict):
        PlanningWorkspaceStore(kit["repo"]).view(
            employee, task_id, now=NOW, mode="text_only",
        )


def test_scope_and_version_validation_prevents_model_callback(kit):
    prompts = []
    client, signer, employee, _, _, _, _ = setup(
        kit, planner=ClosedJsonPlanner(make_proposal_from_prompt(prompts)),
    )
    task_id = new_task(client, signer, employee)
    manager = kit["manager"]
    foreign = employee.model_copy(update={"organization_id": "org_other"})
    actor = employee.model_copy(update={"actor_id": "other_employee"})
    for principal in (manager, foreign, actor):
        assert plan(client, signer, principal, task_id).status_code == 404
    assert plan(client, signer, employee, task_id, ws=99).status_code == 409
    assert plan(client, signer, employee, task_id, extra={
        "granted_operations": ["calendar.delete_event"],
    }).status_code == 422
    assert plan(client, signer, employee, task_id, extra={
        "expected_state_version": -1,
    }).status_code == 422
    assert prompts == []
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED


def test_internal_workflow_is_deterministic_even_if_text_planner_configured(kit):
    calls = []
    client, signer, employee, calendar, internal, _, _ = setup(
        kit, planner=ClosedJsonPlanner(lambda prompt: calls.append(prompt) or "{}"),
    )
    task_id = new_task(client, signer, employee, option="opt_backoffice")
    result = plan(client, signer, employee, task_id)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "PREPARED"
    assert calls == []  # Internal-task preparation uses the strict deterministic gate.
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.READY_TO_COMMIT
    assert calendar.effect_count == internal.effect_count == 0


def test_text_only_ui_controls_and_optional_routes(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    html = client.get("/operator").text
    script = client.get("/operator/assets/app.js").text
    assert 'id="planTaskBtn"' in html and 'id="planningSummary"' in html
    assert '"/plan"' in script and '"/planning"' in script
    assert "textContent" in script
    for bad in ("localStorage", "sessionStorage", "innerHTML", "eval(",
                "document.cookie", "https://"):
        assert bad not in script
    disabled = TestClient(create_app(
        kit["coordinator"], signer, clock=lambda: NOW,
        enable_operator_ui=True,
    ))
    assert disabled.get(
        "/v1/tasks/" + task_id + "/planning",
        headers=auth(signer, employee),
    ).status_code == 404
    assert disabled.post(
        "/v1/tasks/" + task_id + "/plan",
        json=COMMAND, headers=auth(signer, employee),
    ).status_code == 404


def test_planning_demo_never_uses_real_provider():
    result = run_m6_planning_demo()
    assert result["mode"] == "M6_PERSISTENT_TEXT_ONLY_SIMULATED"
    assert result["workspace_status"] == "PREPARED"
    assert result["model_stub_calls"] == 1
    assert result["planning_attempts"] == 1
    assert result["external_effects"] == 0
    assert result["task_phase"] == "AWAITING_APPROVAL"
    assert result["live_provider_writes_enabled"] is False
    assert result["m5_live_acceptance"] == "BLOCKED"
