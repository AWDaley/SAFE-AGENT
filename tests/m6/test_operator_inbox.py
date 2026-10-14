"""M6 local operator inbox, scoped previews and approval safety.

No user-facing route creates a task, issues a bearer token, or dispatches
synthetic/Google effects. HTML/JS exercise only the existing guarded API.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import update

from safe_agent.api import create_app
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import SignedOperatorTokens
from safe_agent.storage.models import TaskRow
from tests.m2.conftest import NOW, arrange
from tests.m3.test_internal_task_execution import make_task_flow

SIGNING_KEY = b"M6_operator_tests_private_local_key_at_least_32_bytes"
ROOT = Path(__file__).resolve().parents[2]


def client_for(kit, *, enable_ui=False):
    signer = SignedOperatorTokens(SIGNING_KEY)
    app = create_app(
        kit["coordinator"], signer,
        clock=lambda: NOW, enable_operator_ui=enable_ui,
    )
    return TestClient(app), signer


def auth(signer, principal):
    return {"Authorization": "Bearer " + signer.issue_for_trusted_operator(principal)}


def pending_calendar(kit):
    coordinator, preview, _, _, proposal = arrange(kit, approval=True)
    result = coordinator.propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=preview.state_version, now=NOW,
    )
    assert result.decision.value == "APPROVAL_REQUIRED"
    return result


def test_optional_operator_ui_uses_strict_csp_and_only_fixed_local_assets(kit):
    web, _ = client_for(kit, enable_ui=False)
    assert web.get("/operator").status_code == 404
    web, _ = client_for(kit, enable_ui=True)
    page = web.get("/operator")
    assert page.status_code == 200
    assert "SAFE-AGENT" in page.text
    assert 'src="/operator/assets/app.js"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert "Content-Security-Policy" in page.headers
    for path, media in (
        ("/operator/assets/app.js", "text/javascript"),
        ("/operator/assets/styles.css", "text/css"),
    ):
        asset = web.get(path)
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "no-store"
        assert asset.headers["content-type"].startswith(media)
        assert asset.headers["x-content-type-options"] == "nosniff"
    assert web.get("/operator/assets/../../pyproject.toml").status_code == 404
    assert web.post("/operator").status_code == 405
    assert web.get("/operator").headers.get("set-cookie") is None


def test_browser_assets_never_persist_credentials_or_render_untrusted_html():
    script = (ROOT / "src/safe_agent/operator_ui/app.js").read_text(encoding="utf-8")
    page = (ROOT / "src/safe_agent/operator_ui/index.html").read_text(encoding="utf-8")
    for forbidden in (
        "localStorage", "sessionStorage", "document.cookie", "innerHTML",
        "outerHTML", "eval(", "new Function(", "http://", "https://",
    ):
        assert forbidden not in script
    assert "textContent" in script
    assert 'credentials: "omit"' in script
    assert 'cache: "no-store"' in script
    assert 'redirect: "error"' in script
    assert 'node("tokenInput").value = "";' in script
    assert '<script src="/operator/assets/app.js" defer></script>' in page
    assert "<script>" not in page
    assert "cdn." not in page


def test_inbox_is_authenticated_scoped_bounded_and_deterministically_paged(kit):
    pending_calendar(kit)
    make_task_flow(kit, approval=False, task_id="task_internal")
    web, signer = client_for(kit, enable_ui=True)
    assert web.get("/v1/tasks").status_code == 401
    manager = auth(signer, kit["manager"])
    page = web.get("/v1/tasks?limit=1", headers=manager)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    result = page.json()
    assert result["schema_version"] == "safe_agent.operator_inbox_page.v1"
    assert len(result["items"]) == 1
    assert result["next_cursor"]
    first = result["items"][0]
    assert first["task_id"] in {"task_internal", "task_m2"}
    more = web.get("/v1/tasks", params={
        "limit": 1, "after": result["next_cursor"],
    }, headers=manager).json()
    assert len(more["items"]) == 1
    assert more["items"][0]["task_id"] != first["task_id"]
    assert more["next_cursor"] is None
    assert {first["task_id"], more["items"][0]["task_id"]} == {
        "task_m2", "task_internal",
    }
    assert "cal_main_private" not in page.text
    assert "cust_c1" not in page.text
    assert web.get("/v1/tasks?limit=51", headers=manager).status_code == 422
    assert web.get("/v1/tasks?after=%2F../../test", headers=manager).status_code == 422


def test_missing_read_scope_and_foreign_org_cannot_discover_tasks(kit):
    pending_calendar(kit)
    web, signer = client_for(kit)
    no_read = kit["manager"].model_copy(update={"scopes": ("task:approve",)})
    assert web.get("/v1/tasks", headers=auth(signer, no_read)).status_code == 403
    other = kit["manager"].model_copy(update={"organization_id": "org_another"})
    page = web.get("/v1/tasks", headers=auth(signer, other))
    assert page.status_code == 200 and page.json()["items"] == []
    assert web.get("/v1/tasks/task_m2/preview", headers=auth(signer, other)).status_code == 404


def test_exact_calendar_preview_has_no_private_binding_or_external_authority(kit):
    pending_calendar(kit)
    web, signer = client_for(kit)
    principal = auth(signer, kit["manager"])
    preview = web.get("/v1/tasks/task_m2/preview", headers=principal)
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    p = preview.json()
    assert p["operation"] == "calendar.create_event"
    assert p["source"] == "synthetic_preview"
    assert p["advisory_only"] is True
    assert p["reserves_slot"] is False
    assert p["approval_required"] is True
    assert p["calendar_ref"] == kit["calendar_ref"]
    assert p["customer_ref"] == kit["customer_ref"]
    assert p["phase"] == "AWAITING_APPROVAL"
    assert p["proposal_hash"].startswith("sha256:")
    assert "cust_c1" not in preview.text and "cal_main_private" not in preview.text
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.AWAITING_APPROVAL


def test_preview_requires_active_pinned_proposal_and_rejects_inconsistent_sql(kit):
    kit["coordinator"].register(kit["principal"], kit["task"], kit["manifest"], now=NOW)
    web, signer = client_for(kit)
    scoped = auth(signer, kit["manager"])
    before = kit["repo"].read_task("task_m2")
    assert web.get("/v1/tasks/task_m2/preview", headers=scoped).status_code == 409
    assert kit["repo"].read_task("task_m2") == before
    # Read-only inbox refuses corrupted denormalized state metadata.
    with kit["repo"]._sessions.begin() as db:
        db.execute(update(TaskRow).where(TaskRow.task_id == "task_m2").values(
            state_version=99,
        ))
    assert web.get("/v1/tasks", headers=scoped).status_code == 409


def test_internal_task_preview_shares_exact_operation_binding(kit):
    make_task_flow(kit, approval=True, task_id="task_internal")
    web, signer = client_for(kit)
    response = web.get("/v1/tasks/task_internal/preview", headers=auth(signer, kit["manager"]))
    assert response.status_code == 200
    data = response.json()
    assert data["operation"] == "tasks.create"
    assert data["task_code"] == "prepare_documents"
    assert data["source"] == "synthetic_preview"
    assert data["approval_required"] is False or data["phase"] == "READY_TO_COMMIT"
    assert data["advisory_only"] is True
    assert "cust_c1" not in response.text
    assert "cal_main_private" not in response.text


def test_operator_approval_requires_separate_actor_exact_hash_and_version(kit):
    decision = pending_calendar(kit)
    web, signer = client_for(kit)
    initiator = auth(signer, kit["principal"])
    manager = auth(signer, kit["manager"])
    state = kit["repo"].read_task("task_m2")
    body = ApprovalInput(
        proposal_hash=decision.proposal_hash,
        expected_state_version=state.state_version,
        approved=True, expires_at=NOW + timedelta(minutes=30),
    ).model_dump(mode="json")
    assert web.post("/v1/tasks/task_m2/approvals", json=body, headers=initiator).status_code == 404
    invalid = dict(body, proposal_hash="sha256:" + "0" * 64)
    assert web.post("/v1/tasks/task_m2/approvals", json=invalid, headers=manager).status_code == 409
    assert kit["repo"].read_task("task_m2").phase is OfficePhase.AWAITING_APPROVAL
    response = web.post("/v1/tasks/task_m2/approvals", json=body, headers=manager)
    assert response.status_code == 200
    assert response.json()["decision"] == "AUTHORIZED"
    assert kit["repo"].read_task("task_m2").phase is OfficePhase.READY_TO_COMMIT
    assert kit["repo"].read_events("task_m2")[-1].kind is EventKind.APPROVAL_GRANTED
    assert web.post("/v1/tasks/task_m2/approvals", json=body, headers=manager).status_code == 409
    assert kit["repo"].replay_task("task_m2").external_effects == 0


def test_operator_denial_is_audited_and_does_not_create_effect(kit):
    decision = pending_calendar(kit)
    web, signer = client_for(kit)
    state = kit["repo"].read_task("task_m2")
    response = web.post("/v1/tasks/task_m2/approvals", headers=auth(signer, kit["manager"]), json={
        "proposal_hash": decision.proposal_hash,
        "expected_state_version": state.state_version,
        "approved": False, "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    })
    assert response.status_code == 200
    assert response.json()["decision"] == "DENIED"
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.REJECTED
    assert kit["repo"].read_events("task_m2")[-1].kind is EventKind.APPROVAL_DENIED
    assert kit["repo"].replay_task("task_m2").external_effects == 0
