"""M6-01b: trusted synthetic assignment, scoped catalog and SQL idempotency."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from safe_agent.api import create_app
from safe_agent.broker.references import BrokerScope, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.control import TaskManifest
from safe_agent.contracts.internal import InternalTaskManifest
from safe_agent.orchestration.task_provisioning import (
    TrustedSyntheticOption,
    TrustedTaskProvisioner,
)
from safe_agent.security.principal import SignedOperatorTokens
from safe_agent.storage.repository import MissingTask, StateConflict
from safe_agent.virtual_office import SyntheticOfficeState
from tests.m2.conftest import NOW, ROOT

SIGNING_KEY = b"m6_intake_operator_token_test_key_not_for_production"


def provisioner(kit) -> TrustedTaskProvisioner:
    state = SyntheticOfficeState.from_fixture(ROOT / "fixtures/synthetic_office/office_state.json")
    return TrustedTaskProvisioner(
        kit["coordinator"], kit["repo"], kit["broker"], state, (
            TrustedSyntheticOption(
                option_id="opt_followup", organization_id="org_demo",
                initiator_id="employee_demo", workflow="schedule_follow_up",
                calendar_resource_id="cal_main_private", customer_resource_id="cust_c1",
                appointment_template_id="follow_up_v1", time_zone="America/New_York",
                requires_approval=True, max_window_minutes=240,
            ),
            TrustedSyntheticOption(
                option_id="opt_backoffice", organization_id="org_demo",
                initiator_id="employee_demo", workflow="create_internal_task",
                customer_resource_id="cust_c1", task_code="review_request",
                time_zone="America/New_York", requires_approval=False,
            ),
        ),
    )


def client_for(kit, *, enabled: bool = True):
    signer = SignedOperatorTokens(SIGNING_KEY)
    app = create_app(
        kit["coordinator"], signer, clock=lambda: NOW, enable_operator_ui=True,
        task_provisioner=provisioner(kit) if enabled else None,
    )
    return TestClient(app), signer


def auth(signer, principal):
    return {"Authorization": "Bearer " + signer.issue_for_trusted_operator(principal)}


def payload(option: str = "opt_followup"):
    return {
        "schema_version": "safe_agent.task_intake.v1",
        "option_id": option,
        "idempotency_key": "req_" + "a" * 32,
        "allowed_start": (NOW + timedelta(days=1)).isoformat(),
        "allowed_end": (NOW + timedelta(days=1, hours=2)).isoformat(),
    }


def test_catalog_is_explicit_and_no_source_or_privileges_leak(kit):
    client, signer = client_for(kit)
    assert client.get("/v1/task-options").status_code == 401
    page = client.get("/v1/task-options", headers=auth(signer, kit["principal"]))
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    result = page.json()
    assert result["schema_version"] == "safe_agent.task_options.v1"
    assert {o["option_id"] for o in result["options"]} == {"opt_followup", "opt_backoffice"}
    for value in ("cal_main_private", "cust_c1", "task:create", "employee_demo"):
        assert value not in page.text
    manager = client.get("/v1/task-options", headers=auth(signer, kit["manager"]))
    assert manager.status_code == 403
    other_actor = kit["principal"].model_copy(update={"actor_id": "other_employee"})
    assert client.get("/v1/task-options", headers=auth(signer, other_actor)).json()["options"] == []
    other_tenant = kit["principal"].model_copy(update={"organization_id": "org_other"})
    assert client.get("/v1/task-options", headers=auth(signer, other_tenant)).json()["options"] == []


def test_calendar_assignment_is_atomic_scope_bound_and_does_not_execute(kit):
    client, signer = client_for(kit)
    credential = auth(signer, kit["principal"])
    response = client.post("/v1/tasks", headers=credential, json=payload())
    assert response.status_code == 201, response.text
    result = response.json()
    task_id = result["task_id"]
    assert result["phase"] == "ASSIGNED"
    assert result["state_version"] == 0
    assert result["provider_write_authorized"] is False
    assert result["synthetic_only"] is True
    for protected in ("cal_main_private", "cust_c1", "provider_token", "manifest_json"):
        assert protected not in response.text
    with kit["repo"].write_task(task_id) as tx:
        manifest = tx.manifest
        assert isinstance(manifest, TaskManifest)
        assert manifest.organization_id == "org_demo"
        assert manifest.initiator_id == "employee_demo"
        assert manifest.permitted_operations == ("calendar.create_event",)
        assert manifest.requires_approval is True
        assert manifest.budget.max_external_effects == 1
        assert manifest.budget.max_effects_per_cycle == 1
        assert manifest.allowed_start.isoformat() == payload()["allowed_start"]
        scope = BrokerScope(
            organization_id="org_demo", initiator_id="employee_demo",
            task_id=task_id, operation="calendar.create_event", now=NOW,
        )
        assert kit["broker"].resolve(
            manifest.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR,
        ) == "cal_main_private"
        assert kit["broker"].resolve(
            manifest.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER,
        ) == "cust_c1"
    private = kit["repo"].load_protected_references(task_id, "org_demo")
    assert len(private) == 2
    assert all(r["record"]["task_id"] == task_id for r in private)
    assert kit["repo"].replay_task(task_id).external_effects == 0
    assert client.get("/v1/tasks/" + task_id + "/preview", headers=credential).status_code == 409
    tasks = client.get("/v1/tasks", headers=credential).json()["items"]
    assert any(t["task_id"] == task_id for t in tasks)


def test_internal_task_assignment_cannot_gain_calendar_authority(kit):
    client, signer = client_for(kit)
    response = client.post(
        "/v1/tasks", headers=auth(signer, kit["principal"]),
        json=payload("opt_backoffice"),
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    with kit["repo"].write_task(task_id) as tx:
        manifest = tx.manifest
        assert isinstance(manifest, InternalTaskManifest)
        assert manifest.task_code == "review_request"
        assert manifest.permitted_operations == ("tasks.create",)
        assert manifest.requires_approval is False
        assert len(kit["repo"].load_protected_references(task_id, "org_demo")) == 1
    assert kit["repo"].replay_task(task_id).external_effects == 0


def test_idempotent_replay_does_not_create_another_task_or_expand_permissions(kit):
    client, signer = client_for(kit)
    headers = auth(signer, kit["principal"])
    first = client.post("/v1/tasks", json=payload(), headers=headers)
    second = client.post("/v1/tasks", json=payload(), headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(client.get("/v1/tasks", headers=headers).json()["items"]) == 1
    altered = payload()
    altered["allowed_end"] = (NOW + timedelta(days=1, hours=3)).isoformat()
    assert client.post("/v1/tasks", json=altered, headers=headers).status_code == 409
    different_actor = kit["principal"].model_copy(update={"actor_id": "other_employee"})
    assert client.post("/v1/tasks", json=payload(), headers=auth(signer, different_actor)).status_code == 404


@pytest.mark.parametrize("change,expected", [
    ({"organization_id": "org_demo"}, 422),
    ({"initiator_id": "employee_demo"}, 422),
    ({"calendar_resource_id": "cal_main_private"}, 422),
    ({"budget": {"max_external_effects": 99}}, 422),
    ({"permitted_operations": ["calendar.delete_event"]}, 422),
    ({"option_id": "cal_main_private"}, 404),
    ({"idempotency_key": "weak"}, 422),
    ({"allowed_start": "2026-10-15T12:00:00"}, 422),
    ({"allowed_start": 12345}, 422),
    ({"allowed_start": True}, 422),
    ({"allowed_start": {"unexpected": "object"}}, 422),
    ({"allowed_end": (NOW + timedelta(days=40)).isoformat()}, 422),
    ({"allowed_end": (NOW + timedelta(days=1, hours=5)).isoformat()}, 422),
])
def test_malformed_unauthorized_or_out_of_policy_intake_never_creates_task(
    kit, change, expected,
):
    client, signer = client_for(kit)
    request = payload()
    request.update(change)
    result = client.post("/v1/tasks", json=request, headers=auth(signer, kit["principal"]))
    assert result.status_code == expected, result.text
    assert client.get("/v1/tasks", headers=auth(signer, kit["principal"])).json()["items"] == []


def test_missing_scope_and_disabled_intake_cannot_create_task(kit):
    client, signer = client_for(kit)
    assert client.post("/v1/tasks", json=payload()).status_code == 401
    assert client.post(
        "/v1/tasks", json=payload(), headers=auth(signer, kit["manager"]),
    ).status_code == 403
    client, signer = client_for(kit, enabled=False)
    assert client.post(
        "/v1/tasks", json=payload(), headers=auth(signer, kit["principal"]),
    ).status_code == 405
    assert client.get("/v1/task-options", headers=auth(signer, kit["principal"])).status_code == 404


def test_intake_unique_violation_rolls_back_new_task_and_all_private_refs(kit):
    client, signer = client_for(kit)
    assert client.post("/v1/tasks", json=payload(),
                       headers=auth(signer, kit["principal"])).status_code == 201
    # A second trusted registration with a reused SQL idempotency key cannot
    # partially leave behind a manifest, task or protected reference row.
    with pytest.raises(IntegrityError):
        kit["coordinator"].register(
            kit["principal"], kit["task"], kit["manifest"], now=NOW,
            protected_references=kit["broker"].trusted_export(task_id="task_m2"),
            intake_key=payload()["idempotency_key"], intake_hash=sha256(payload()),
        )
    with pytest.raises(MissingTask):
        kit["repo"].read_task("task_m2")
    assert kit["repo"].load_protected_references(
        client.get("/v1/tasks", headers=auth(signer, kit["principal"])).json()["items"][0]["task_id"],
        "org_demo",
    )


def test_ui_contains_opt_in_safe_assignment_form(kit):
    client, _ = client_for(kit)
    page = client.get("/operator")
    assert page.status_code == 200
    assert 'id="assignmentPanel"' in page.text
    assert 'id="assignmentForm"' in page.text
    assert "No live provider actions" in page.text
    script = client.get("/operator/assets/app.js").text
    assert '"/v1/task-options"' in script
    assert '"/v1/tasks", "POST"' in script
    assert "textContent" in script
    for disallowed in ("localStorage", "sessionStorage", "innerHTML", "document.cookie"):
        assert disallowed not in script


def test_invalid_private_binding_prevents_task_registration(kit):
    refs = kit["broker"].trusted_export(task_id="task_m2")
    damaged = [dict(x) for x in refs]
    damaged[0]["handle"] = "ref_wrong"
    with pytest.raises(StateConflict):
        kit["coordinator"].register(
            kit["principal"], kit["task"], kit["manifest"], now=NOW,
            protected_references=tuple(damaged),
            intake_key="req_" + "c" * 32, intake_hash=sha256(payload()),
        )
    with pytest.raises(MissingTask):
        kit["repo"].read_task("task_m2")
