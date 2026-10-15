"""M6-01c acceptance: synthetic task assignment -> review -> outbox -> readback.

No test has Google credentials or a model-facing provider execution path.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.api import create_app
from safe_agent.canonical import sha256
from safe_agent.contracts.control import ActionDecision, DecisionStatus, ExecutionOutcome
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.execution.recovery import FaultPoint, SyntheticRecoveryWorker
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.security.principal import SignedOperatorTokens
from safe_agent.storage.models import LedgerRow, ProposalRow, ReferenceRow, SimulationRow
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.tal.office_gate import OfficeGate
from tests.m2.conftest import NOW
from tests.m6.test_task_intake import (
    SIGNING_KEY,
    auth,
    payload,
    provisioner,
)

ROOT = Path(__file__).resolve().parents[2]
CMD = {"schema_version": "safe_agent.synthetic_workflow_command.v1",
       "expected_state_version": 0}


def rig(kit, *, with_gateway: bool = True):
    """Reuses exactly the application SQL/provider objects, never live Google."""
    source = kit["office"]._source
    durable = DurableFakeProvider(kit["repo"].engine, source)
    calendar = FakeCalendarAdapter(source, durable_store=durable)
    tasks = FakeInternalTaskAdapter(source, durable_store=durable)
    gateway = SyntheticExecutionGateway(
        kit["repo"], kit["office"], OfficeGate(kit["broker"]),
        kit["broker"], calendar, task_adapter=tasks,
    )
    workflow = TrustedSyntheticWorkflow(
        kit["repo"], kit["office"], kit["broker"], kit["coordinator"],
        gateway=gateway if with_gateway else None,
    )
    signer = SignedOperatorTokens(SIGNING_KEY)
    app = create_app(
        kit["coordinator"], signer, clock=lambda: NOW,
        task_provisioner=provisioner(kit), synthetic_workflow=workflow,
        enable_operator_ui=True,
    )
    employee = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    return TestClient(app), signer, employee, calendar, tasks, durable


def new_task(client, signer, employee, *, option="opt_followup", params=None):
    data = payload(option)
    if params:
        data.update(params)
    response = client.post("/v1/tasks", headers=auth(signer, employee), json=data)
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def prepare(client, signer, employee, task_id, *, version=0):
    return client.post(
        "/v1/tasks/" + task_id + "/prepare",
        headers=auth(signer, employee),
        json={**CMD, "expected_state_version": version},
    )


def queue(client, signer, employee, task_id, *, version):
    return client.post(
        "/v1/tasks/" + task_id + "/queue-synthetic",
        headers=auth(signer, employee),
        json={**CMD, "expected_state_version": version},
    )


def grant(client, signer, manager, task_id, prepared):
    state = client.get(
        "/v1/tasks/" + task_id, headers=auth(signer, manager),
    ).json()
    assert state["phase"] == "AWAITING_APPROVAL"
    response = client.post(
        "/v1/tasks/" + task_id + "/approvals",
        headers=auth(signer, manager),
        json={
            "proposal_hash": prepared["proposal_hash"],
            "expected_state_version": state["state_version"],
            "approved": True,
            "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["state_version"]


def test_calendar_full_lifecycle_requires_review_and_independent_worker(kit):
    client, signer, employee, calendar, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    employee_token = auth(signer, employee)
    initial = kit["repo"].read_task(task_id)
    assert initial.phase is OfficePhase.ASSIGNED

    proposed = prepare(client, signer, employee, task_id)
    assert proposed.status_code == 200, proposed.text
    data = proposed.json()
    assert data["schema_version"] == "safe_agent.synthetic_preparation.v1"
    assert data["phase"] == "AWAITING_APPROVAL"
    assert data["approval_required"] is True
    assert data["provider_effect_performed"] is False
    assert data["proposal_hash"].startswith("sha256:")
    assert calendar.effect_count == 0
    snapshot = kit["repo"].replay_task(task_id)
    assert snapshot.phase is OfficePhase.AWAITING_APPROVAL
    assert snapshot.cycles_used == 1 and snapshot.external_effects == 0
    events = kit["repo"].read_events(task_id)
    assert [e.kind for e in events] == [
        EventKind.TASK_CREATED,
        EventKind.PREVIEW_REGISTERED,
        EventKind.PROPOSAL_ACCEPTED,
        EventKind.APPROVAL_REQUIRED,
    ]
    assert prepare(client, signer, employee, task_id).status_code == 409
    assert len(kit["repo"].read_events(task_id)) == len(events)

    preview = client.get(
        "/v1/tasks/" + task_id + "/preview",
        headers=auth(signer, kit["manager"]),
    )
    assert preview.status_code == 200
    assert preview.json()["proposal_hash"] == data["proposal_hash"]
    assert preview.json()["source"] == "synthetic_preview"
    assert preview.json()["reserves_slot"] is False
    for private in ("cal_main_private", "cust_c1", "@", "refresh_token"):
        assert private not in preview.text

    # Neither admission nor a still-unapproved proposal can reach the fake writer.
    assert queue(client, signer, employee, task_id,
                 version=data["state_version"]).status_code == 409
    approved_version = grant(client, signer, kit["manager"], task_id, data)
    staged = queue(client, signer, employee, task_id, version=approved_version)
    assert staged.status_code == 200, staged.text
    assert staged.json()["phase"] == "EXECUTING"
    assert staged.json()["queued_only"] is True
    assert staged.json()["provider_effect_performed"] is False
    assert calendar.effect_count == 0
    assert kit["repo"].read_outbox(task_id)["status"] == "QUEUED"
    assert queue(client, signer, employee, task_id,
                 version=approved_version).status_code == 409

    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"],
                                     calendar, office=kit["office"])
    receipt = worker.process_one(task_id=task_id, now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert calendar.effect_count == 1
    completed = kit["repo"].replay_task(task_id)
    assert completed.phase is OfficePhase.COMPLETED
    assert completed.external_effects == 1
    assert completed.effect_ref == receipt.effect_ref
    assert completed.verification_hash is not None
    assert kit["repo"].read_outbox(task_id)["status"] == "VERIFIED"
    assert kit["repo"].read_events(task_id)[-1].kind is EventKind.ACTION_VERIFIED
    assert worker.process_one(task_id=task_id, now=NOW) is None
    assert calendar.effect_count == 1
    assert client.get(
        "/v1/tasks/" + task_id, headers=employee_token,
    ).json()["phase"] == "COMPLETED"


def test_internal_task_lifecycle_uses_same_gate_and_synthetic_outbox(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = new_task(client, signer, employee, option="opt_backoffice")
    response = prepare(client, signer, employee, task_id)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["phase"] == "READY_TO_COMMIT"
    assert data["approval_required"] is False
    assert kit["repo"].replay_task(task_id).cycles_used == 1
    preview = client.get(
        "/v1/tasks/" + task_id + "/preview", headers=auth(signer, employee),
    )
    assert preview.status_code == 200
    assert preview.json()["operation"] == "tasks.create"
    assert preview.json()["task_code"] == "review_request"
    assert calendar.effect_count == 0 and internal.effect_count == 0

    queued = queue(client, signer, employee, task_id,
                   version=data["state_version"])
    assert queued.status_code == 200, queued.text
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal,
        office=kit["office"],
    )
    result = worker.process_one(task_id=task_id, now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.COMMITTED
    assert internal.effect_count == 1 and calendar.effect_count == 0
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert kit["repo"].replay_task(task_id).verification_hash


def test_recovery_after_response_loss_does_not_duplicate_booking(kit):
    client, signer, employee, calendar, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    prepared = prepare(client, signer, employee, task_id).json()
    version = grant(client, signer, kit["manager"], task_id, prepared)
    assert queue(client, signer, employee, task_id, version=version).status_code == 200
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"],
                                     calendar, office=kit["office"])
    uncertain = worker.process_one(task_id=task_id, now=NOW,
                                   fault=FaultPoint.RESPONSE_LOST)
    assert uncertain is not None and uncertain.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.EXECUTING
    assert kit["repo"].read_outbox(task_id)["status"] == "OUTCOME_UNKNOWN"
    assert calendar.effect_count == 1
    verified = worker.process_one(task_id=task_id, now=NOW + timedelta(seconds=6))
    assert verified is not None and verified.outcome is ExecutionOutcome.COMMITTED
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert calendar.effect_count == 1


@pytest.mark.parametrize("wrong", ["reviewer", "foreign_actor", "foreign_tenant"])
def test_prepare_and_queue_deny_other_principals_without_effect(kit, wrong):
    client, signer, employee, calendar, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    if wrong == "reviewer":
        impostor = kit["manager"]
    elif wrong == "foreign_actor":
        impostor = employee.model_copy(update={"actor_id": "other_actor"})
    else:
        impostor = employee.model_copy(update={"organization_id": "org_other"})
    assert prepare(client, signer, impostor, task_id).status_code == 404
    response = prepare(client, signer, employee, task_id)
    assert response.status_code == 200
    assert queue(
        client, signer, impostor, task_id,
        version=response.json()["state_version"],
    ).status_code == 404
    assert calendar.effect_count == 0
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.AWAITING_APPROVAL


def test_prepare_rejects_naive_request_stale_version_and_disabled_routes(kit):
    client, signer, employee, _, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    url = "/v1/tasks/" + task_id + "/prepare"
    assert client.post(url, headers=auth(signer, employee),
                       json={"expected_state_version": 0}).status_code == 200
    assert client.post(
        url, headers=auth(signer, employee),
        json={**CMD, "expected_state_version": 0, "grant": "any"},
    ).status_code == 422
    assert client.post(
        url, headers=auth(signer, employee),
        json={**CMD, "expected_state_version": -1},
    ).status_code == 422
    assert client.post(url, headers=auth(signer, employee),
                       json=CMD).status_code == 409
    # The optional workflow service must not change default API endpoints.
    disabled = create_app(
        kit["coordinator"], SignedOperatorTokens(SIGNING_KEY),
        clock=lambda: NOW, enable_operator_ui=True,
    )
    other = TestClient(disabled)
    assert other.post(url, headers=auth(signer, employee), json=CMD).status_code == 404
    assert other.post(
        "/v1/tasks/" + task_id + "/queue-synthetic",
        headers=auth(signer, employee), json=CMD,
    ).status_code == 404


def test_no_available_slot_keeps_task_assigned_and_no_preview(kit):
    client, signer, employee, calendar, _, _ = rig(kit)
    task_id = new_task(
        client, signer, employee,
        params={
            "allowed_start": "2026-10-15T01:00:00-04:00",
            "allowed_end": "2026-10-15T03:00:00-04:00",
        },
    )
    result = prepare(client, signer, employee, task_id)
    assert result.status_code == 409
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task_id)) == 1
    assert calendar.effect_count == 0


def test_atomic_policy_denial_rolls_back_simulation_proposal_and_ledger(kit, monkeypatch):
    client, signer, employee, calendar, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    gate = kit["coordinator"]._gate

    def deny_candidate(**params):
        return ActionDecision(
            decision=DecisionStatus.DENIED,
            manifest_hash=params["task"].manifest_hash,
            proposal_hash=sha256(params["proposal"]),
            reason_code="POLICY_TEST_DENIAL",
            state_version=params["snapshot"].state_version,
        )

    monkeypatch.setattr(gate, "evaluate", deny_candidate)
    result = prepare(client, signer, employee, task_id)
    assert result.status_code == 409
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED
    assert len(kit["repo"].read_events(task_id)) == 1
    with kit["repo"]._sessions() as session:
        assert session.scalars(select(SimulationRow)).all() == []
        assert session.scalars(select(ProposalRow)).all() == []
        assert len(session.scalars(select(LedgerRow)).all()) == 1
    assert calendar.effect_count == 0


def test_tampered_private_reference_fails_before_planning(kit):
    client, signer, employee, _, _, _ = rig(kit)
    task_id = new_task(client, signer, employee)
    private = kit["repo"].load_protected_references(task_id, "org_demo")
    handle = private[0]["handle"]
    with kit["repo"]._sessions.begin() as db:
        changed = dict(private[0])
        changed["mac"] = "0" * 64
        db.execute(
            update(ReferenceRow).where(ReferenceRow.handle == handle)
            .values(binding_json=changed)
        )
    assert prepare(client, signer, employee, task_id).status_code == 409
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.ASSIGNED


def test_queue_needs_privileged_gateway_and_exact_execution_scope(kit):
    client, signer, employee, calendar, _, _ = rig(kit, with_gateway=False)
    task_id = new_task(client, signer, employee, option="opt_backoffice")
    prepared = prepare(client, signer, employee, task_id)
    assert prepared.status_code == 200
    assert queue(client, signer, employee, task_id,
                 version=prepared.json()["state_version"]).status_code == 409
    no_execute = kit["principal"]
    assert queue(client, signer, no_execute, task_id,
                 version=prepared.json()["state_version"]).status_code == 404
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.READY_TO_COMMIT
    assert calendar.effect_count == 0


def test_operator_page_has_queued_only_controls_and_no_provider_tool_path(kit):
    client, _, _, _, _, _ = rig(kit)
    markup = client.get("/operator").text
    js = client.get("/operator/assets/app.js").text
    assert 'id="prepareTaskBtn"' in markup
    assert 'id="queueTaskBtn"' in markup
    assert '"/prepare"' in js and '"/queue-synthetic"' in js
    assert "textContent" in js
    for forbidden in ("innerHTML", "eval(", "localStorage", "sessionStorage",
                      "document.cookie", "https://"):
        assert forbidden not in js
