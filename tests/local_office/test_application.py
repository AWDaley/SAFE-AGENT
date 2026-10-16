"""Application assembly/restart tests. Transport substitution is confined to tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.local_office import application
from safe_agent.local_office.__main__ import run_worker
from safe_agent.local_office.application import create_local_api, issue_session
from safe_agent.local_office.state import initialize, open_office
from safe_agent.local_office.workspace import LocalOfficeBlocked, workspace_lock


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "office"
    initialize(root, Path(__file__).resolve().parents[2] / "migrations")
    return root


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(application, "require_environment", lambda: None)
    def send(self, request, claim, marker):
        seen.append(request.request_id)
        return OfflineMockInferenceTransport().infer(request)
    monkeypatch.setattr(CoordinatedUDSInferenceTransport, "send", send)
    return seen


def client(root):
    return TestClient(create_local_api(root), base_url="http://127.0.0.1:8765")


def auth(root, role="requester"):
    return {"Authorization": "Bearer " + issue_session(root, role)}


def create_task(api, root, nonce="a"):
    start = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    while start.weekday() > 4:
        start += timedelta(days=1)
    response = api.post("/v1/tasks", headers=auth(root), json={
        "option_id": "local_follow_up", "idempotency_key": "req_" + nonce * 32,
        "allowed_start": start.isoformat(), "allowed_end": (start + timedelta(hours=2)).isoformat(),
    })
    assert response.status_code == 201, response.text
    return response.json()["task_id"]


def plan(api, root, task):
    response = api.post(f"/v1/tasks/{task}/plan", headers=auth(root), json={
        "expected_state_version": 0, "expected_workspace_version": 0,
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PREPARED"
    return response.json()


def approve(api, root, task, prepared, *, role="approver"):
    snapshot = api.get(f"/v1/tasks/{task}", headers=auth(root, role))
    assert snapshot.status_code == 200, snapshot.text
    return api.post(f"/v1/tasks/{task}/approvals", headers=auth(root, role), json={
        "proposal_hash": prepared["proposal_hash"],
        "expected_state_version": snapshot.json()["state_version"], "approved": True,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    })


def test_restart_before_approval_and_queue_restores_original_projection(workspace, calls):
    with client(workspace) as first:
        task = create_task(first, workspace)
        prepared = plan(first, workspace, task)
        assert approve(first, workspace, task, prepared, role="requester").status_code == 404
    with client(workspace) as second:
        granted = approve(second, workspace, task, prepared)
        assert granted.status_code == 200, granted.text
        response = second.post(f"/v1/tasks/{task}/queue-synthetic", headers=auth(workspace),
            json={"expected_state_version": granted.json()["state_version"]})
        assert response.status_code == 200, response.text
        assert response.json()["provider_effect_performed"] is False
    local = open_office(workspace)
    assert local.calendar.effect_count == 0
    local.close()
    assert run_worker(workspace)["committed_receipts"] == 1
    assert run_worker(workspace)["claims_processed"] == 0
    with client(workspace) as final:
        assert final.get(f"/v1/tasks/{task}", headers=auth(workspace)).json()["phase"] == "COMPLETED"
    assert len(calls) == 1


def test_running_api_refreshes_from_persisted_provider_after_worker(workspace, calls):
    with client(workspace) as api:
        for nonce in ("a", "b"):
            task = create_task(api, workspace, nonce)
            prepared = plan(api, workspace, task)
            granted = approve(api, workspace, task, prepared)
            assert granted.status_code == 200, granted.text
            response = api.post(f"/v1/tasks/{task}/queue-synthetic", headers=auth(workspace),
                json={"expected_state_version": granted.json()["state_version"]})
            assert response.status_code == 200, response.text
            assert run_worker(workspace)["committed_receipts"] == 1
    saved = open_office(workspace)
    assert saved.calendar.effect_count == 2
    saved.close()
    assert len(calls) == 2


def test_approval_alone_never_starts_worker_or_creates_effect(workspace, calls):
    with client(workspace) as api:
        task = create_task(api, workspace)
        prepared = plan(api, workspace, task)
        assert approve(api, workspace, task, prepared).status_code == 200
        assert api.app.state.local_office.calendar.effect_count == 0
        assert run_worker(workspace)["claims_processed"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["/v1/tasks", "/v1/task-options", "/v1/tasks/missing/planning"])
def test_missing_auth_denied_before_task_lookup(workspace, calls, path):
    with client(workspace) as api:
        assert api.get(path).status_code == 401
    assert calls == []


@pytest.mark.parametrize("headers,status", [
    ({"Host": "attacker.invalid:8765"}, 400),
    ({"Origin": "https://attacker.invalid"}, 403),
    ({"Host": "localhost:8765", "Origin": "http://127.0.0.1:8765"}, 403),
])
def test_local_host_and_origin_enforced(workspace, calls, headers, status):
    with client(workspace) as api:
        assert api.get("/operator", headers=headers).status_code == status
    assert calls == []


def test_no_unauthed_session_worker_or_provider_route(workspace, calls):
    with client(workspace) as api:
        for path in ("/session", "/worker", "/v1/providers", "/v1/execute"):
            assert api.post(path).status_code in (401, 404)
        task = create_task(api, workspace)
        denied = api.post(f"/v1/tasks/{task}/prepare", headers=auth(workspace),
                          json={"expected_state_version": 0})
        assert denied.status_code == 409
    assert calls == []


def test_api_busy_returns_no_retry_or_effect(workspace, calls):
    with client(workspace) as api:
        with workspace_lock(workspace):
            response = api.get("/v1/tasks", headers=auth(workspace))
            assert response.status_code == 503
            assert "workspace unavailable" in response.text
    assert calls == []


def test_liveness_is_not_readiness_without_sidecar(workspace, calls):
    with client(workspace) as api:
        assert api.get("/healthz").status_code == 200
        result = api.get("/readyz")
        assert result.status_code == 503
        assert result.json()["business_execution_authorized"] is False
        assert result.json()["production_authorized"] is False
    assert calls == []


def test_preflight_failure_happens_before_workspace_access(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "inspect_environment", lambda: SimpleNamespace(status="BLOCKED"))
    root = tmp_path / "not-created"
    with pytest.raises(LocalOfficeBlocked, match="LOCKED_ENVIRONMENT_REQUIRED"):
        create_local_api(root)
    assert not root.exists()
