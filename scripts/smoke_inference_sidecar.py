"""Prove M7-04 runs as a real separate process on a permissioned UDS.

Uses only disposable synthetic task data and a disposable workload credential.
No TCP connection, external model provider, SQL ledger or business effect.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import sha256
from safe_agent.contracts.inference import InferenceRequest
from safe_agent.contracts.office import AvailableSlot, Projection

WORKLOAD_KEY = "synthetic_offline_udsonly_service_token_0123456789abcdef"
HOST = "inference.example.com"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="safe_infer_service_smoke_") as tmp:
        work = Path(tmp)
        work.chmod(0o700)
        token_file = work / "workload.token"
        token_file.write_text(WORKLOAD_KEY, encoding="ascii")
        token_file.chmod(0o600)
        socket_path = work / "inference.sock"

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(work),
            "SAFE_INFER_SOCKET_DIR": str(work),
            "SAFE_INFER_WORKLOAD_TOKEN_FILE": str(token_file),
            "SAFE_INFER_HOST": HOST,
            "SAFE_INFER_ROUTE": "route_test_only",
            "SAFE_INFER_MODEL_REVISION": "mock_v1",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "safe_agent.inference_service"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(work), close_fds=True, start_new_session=True,
        )
        try:
            started = time.monotonic()
            while not socket_path.exists():
                if process.poll() is not None:
                    raise AssertionError("inference-only UDS service did not start")
                if time.monotonic() - started > 10:
                    raise AssertionError("inference-only UDS launch deadline exceeded")
                time.sleep(0.04)
            if not socket_path.is_socket() or socket_path.stat().st_mode & 0o077:
                raise AssertionError("inference service socket permissions unsafe")

            # Construct a fresh, entirely synthetic task observation; the
            # standalone script does not import the repository test package.
            actual = datetime.now(UTC)
            allowed_start = actual + timedelta(days=1)
            allowed_end = allowed_start + timedelta(hours=2)
            context = SafeTaskContext(
                task_id="task_offline_smoke",
                organization_id="org_demo",
                calendar_ref="ref_calendar", customer_ref="ref_customer",
                allowed_start=allowed_start, allowed_end=allowed_end,
                duration_minutes=30, time_zone="America/New_York",
                template_id="follow_up_v1",
                simulation_ref="simulation_pending",
            )
            projection = Projection(
                task_id=context.task_id,
                organization_id=context.organization_id,
                calendar_ref=context.calendar_ref, revision="proj_offline_smoke",
                fetched_at=actual - timedelta(seconds=1),
                expires_at=actual + timedelta(minutes=2),
                free_slots=(AvailableSlot(
                    start_at=allowed_start, end_at=allowed_end,
                ),),
            )
            request = InferenceRequest(
                request_id="inf_" + "e" * 32,
                route_id="route_test_only",
                model_revision="mock_v1",
                prompt_digest=sha256({
                    "instruction": PLANNER_INSTRUCTION,
                    "task": context, "availability": projection,
                }),
                issued_at=actual, deadline_at=actual + timedelta(seconds=15),
                task=context, availability=projection,
            )
            transport = httpx.HTTPTransport(uds=str(socket_path), retries=0)
            with httpx.Client(
                transport=transport, base_url="http://" + HOST, timeout=5.0,
            ) as client:
                bad = client.post(
                    "/v1/infer", content=request.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                if bad.status_code != 401:
                    raise AssertionError("unauthenticated inference admitted")
                headers = {
                    "Authorization": "Bearer " + WORKLOAD_KEY,
                    "Content-Type": "application/json",
                }
                result = client.post(
                    "/v1/infer", headers=headers,
                    content=request.model_dump_json(),
                )
                if result.status_code != 200:
                    raise AssertionError("synthetic inference-only UDS request denied")
                data = result.json()
                if (data["request_id"] != request.request_id
                        or data["reported_usage"]["source"] != "synthetic_mock"):
                    raise AssertionError("inference-only response binding failed")
                repeated = client.post(
                    "/v1/infer", headers=headers,
                    content=request.model_dump_json(),
                )
                if repeated.status_code != 409:
                    raise AssertionError("inference-only request replay admitted")
                status = client.get("/internal/status", headers=headers)
                if status.status_code != 200 or status.json()["completed"] != 1:
                    raise AssertionError("inference service operational status invalid")
                if status.json()["real_provider_enabled"] is not False:
                    raise AssertionError("hosted inference unexpectedly enabled")
            print("M7_OFFLINE_INFERENCE_SERVICE_UDS_ONLY=PASS")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
