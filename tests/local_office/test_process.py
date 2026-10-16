"""Installed CLI + real loopback API/UDS/worker acceptance, not mock HTTP tests."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_LOCAL_PROCESS") != "1",
    reason="requires dedicated installed, hash-locked local-runtime acceptance",
)
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_installed_launcher_survives_api_restart_and_separate_worker():
    cli = os.environ["SAFE_AGENT_LOCAL_CLI"]
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("SAFE_INFER_", "SAFE_AGENT_DATABASE"))
                   and k not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="slo_", dir="/tmp") as temporary:
        root = Path(temporary) / "office"
        def command(*args, success=True):
            result = subprocess.run([cli, "--workspace", str(root), *args], cwd="/tmp",
                env=environment, capture_output=True, text=True, timeout=40, check=False)
            assert (result.returncode == 0) == success, result.stderr
            return result.stdout.strip()
        def start(*args):
            return subprocess.Popen([cli, "--workspace", str(root), *args], cwd="/tmp",
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True)
        def stop(process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        base = "http://127.0.0.1:" + str(port)
        command("init", "--migrations", str(MIGRATIONS))
        requester = {"Authorization": "Bearer " + command("session", "--role", "requester")}
        approver = {"Authorization": "Bearer " + command("session", "--role", "approver")}
        api = sidecar = None
        try:
            api = start("serve", "--port", str(port))
            with httpx.Client(base_url=base, timeout=12, trust_env=False,
                              follow_redirects=False) as http:
                def wait_for(path, expected=200):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        assert api is not None and api.poll() is None, "local API exited"
                        try:
                            response = http.get(path)
                            if response.status_code == expected:
                                return response
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.05)
                    raise AssertionError("local readiness deadline exceeded")
                wait_for("/healthz")
                assert http.get("/readyz").status_code == 503
                assert http.get("/v1/tasks").status_code == 401
                assert http.get("/operator").status_code == 200
                sidecar = start("sidecar")
                ready = wait_for("/readyz")
                assert ready.json()["business_execution_authorized"] is False
                assert ready.json()["production_authorized"] is False
                assert (root / "inference/inference.sock").is_socket()
                assert (root / "inference/inference.sock").stat().st_mode & 0o077 == 0
                start_at = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).replace(
                    hour=10, minute=0, second=0, microsecond=0)
                while start_at.weekday() > 4:
                    start_at += timedelta(days=1)
                def assign(nonce):
                    response = http.post("/v1/tasks", headers=requester, json={
                        "option_id": "local_follow_up", "idempotency_key": "req_" + nonce * 32,
                        "allowed_start": start_at.isoformat(),
                        "allowed_end": (start_at + timedelta(hours=2)).isoformat(),
                    })
                    assert response.status_code == 201, response.text
                    return response.json()["task_id"]
                def plan(task):
                    response = http.post(f"/v1/tasks/{task}/plan", headers=requester,
                        json={"expected_state_version": 0, "expected_workspace_version": 0})
                    assert response.status_code == 200, response.text
                    assert response.json()["status"] == "PREPARED"
                    return response.json()
                def approve_and_queue(task, proposal):
                    state = http.get(f"/v1/tasks/{task}", headers=approver).json()
                    granted = http.post(f"/v1/tasks/{task}/approvals", headers=approver, json={
                        "proposal_hash": proposal["proposal_hash"], "approved": True,
                        "expected_state_version": state["state_version"],
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    })
                    assert granted.status_code == 200, granted.text
                    queued = http.post(f"/v1/tasks/{task}/queue-synthetic", headers=requester,
                        json={"expected_state_version": granted.json()["state_version"]})
                    assert queued.status_code == 200, queued.text
                    assert queued.json()["provider_effect_performed"] is False
                first_task = assign("a")
                first_proposal = plan(first_task)
                # Abrupt actual API exit: the next process has no broker or projection cache.
                api.kill()
                api.wait(timeout=5)
                api = start("serve", "--port", str(port))
                wait_for("/readyz")
                saved = http.get(f"/v1/tasks/{first_task}/planning", headers=requester)
                assert saved.json()["proposal_hash"] == first_proposal["proposal_hash"]
                approve_and_queue(first_task, first_proposal)
                with sqlite3.connect(root / "office.sqlite") as db:
                    assert db.execute("SELECT count(*) FROM safe_fake_provider_effects").fetchone() == (0,)
                    assert db.execute("SELECT count(*) FROM safe_inference_invocations").fetchone() == (1,)
                # Execution is independent of inference availability and does not retry inference.
                stop(sidecar)
                sidecar = None
                command("worker")
                assert http.get(f"/v1/tasks/{first_task}", headers=requester).json()["phase"] == "COMPLETED"
                command("worker")
                sidecar = start("sidecar")
                wait_for("/readyz")
                second_task = assign("b")
                second_proposal = plan(second_task)
                approve_and_queue(second_task, second_proposal)
                command("worker")
                assert http.get(f"/v1/tasks/{second_task}", headers=requester).json()["phase"] == "COMPLETED"
                with sqlite3.connect(root / "office.sqlite") as db:
                    assert db.execute("SELECT count(*) FROM safe_fake_provider_effects").fetchone() == (2,)
                    assert db.execute("SELECT count(*) FROM safe_inference_invocations").fetchone() == (2,)
                    assert db.execute("SELECT count(*) FROM safe_inference_budget_reservations").fetchone() == (2,)
                # A duplicate initializer cannot erase this successful history.
                before = (root / "office.sqlite").read_bytes()
                command("init", "--migrations", str(MIGRATIONS), success=False)
                assert (root / "office.sqlite").read_bytes() == before
                assert "PERSISTED_STATE_AVAILABLE" in command("status")
        finally:
            stop(api)
            stop(sidecar)



def test_installed_cli_requires_explicit_upgrade_and_can_rollback():
    from tests.local_office.test_maintenance import legacy_workspace

    cli = os.environ["SAFE_AGENT_LOCAL_CLI"]
    environment = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("SAFE_INFER_", "SAFE_AGENT_DATABASE"))
        and k not in {"PYTHONPATH", "PYTHONHOME"}
    }
    with tempfile.TemporaryDirectory(prefix="slo_upgrade_", dir="/tmp") as temporary:
        parent = Path(temporary)
        root = legacy_workspace(parent)
        backup = parent / "pre-0014-backup"

        def command(*args, success=True):
            result = subprocess.run(
                [cli, "--workspace", str(root), *args],
                cwd="/tmp", env=environment, capture_output=True,
                text=True, timeout=45, check=False,
            )
            assert (result.returncode == 0) == success, result.stderr
            return result

        blocked = command("status", success=False)
        assert "BLOCKED" in blocked.stderr

        upgraded = command(
            "upgrade", "--migrations", str(MIGRATIONS),
            "--backup", str(backup),
        )
        report = json.loads(upgraded.stdout)
        assert report["status"] == "UPGRADED"
        assert report["source_head"] == "0013_coordinated_planning"
        assert report["target_head"] == "0014_billing_verifier_identity"
        assert json.loads(command("status").stdout)["status"] == "PERSISTED_STATE_AVAILABLE"

        rolled_back = command("rollback", "--backup", str(backup))
        report = json.loads(rolled_back.stdout)
        assert report["status"] == "ROLLED_BACK"
        assert report["migration_head"] == "0013_coordinated_planning"
        assert report["requires_reviewed_upgrade_before_current_runtime"] is True
        assert "BLOCKED" in command("status", success=False).stderr
