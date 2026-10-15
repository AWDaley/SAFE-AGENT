"""Real process acceptance for the offline coordinator and private UDS sidecar.

Uses disposable synthetic SQL/credentials. No live network or provider calls.
Exercises response-loss/restart semantics outside of the model's narration.
"""
from __future__ import annotations

import multiprocessing
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.inference_dispatch import TrustedInferenceDispatchStore
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import assigned, policy

HOST = "inference.example.com"


def _send_and_crash(url, args, path, token, key) -> None:
    # A distinct trusted caller process exits AFTER the sidecar executed, before
    # recording finish. No model-origin success report is used as evidence.
    repo = LedgerRepository.testing_sqlite(url)
    admission = TrustedInferenceAdmission(repo, policy=policy())
    store = TrustedInferenceDispatchStore(admission)
    admission.admit(*args)
    claim = store.claim(*args)
    request, marker = store.start(*args, claim)
    wire = CoordinatedUDSInferenceTransport(path, expected_host=HOST,
                                           workload_token=token, dispatch_key=key)
    wire.send(request, claim, marker)
    os._exit(73)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="safe_dispatch_") as tmp:
        work = Path(tmp)
        work.chmod(0o700)
        token, key = SecretStr(secrets.token_hex(32)), SecretStr(secrets.token_hex(32))
        for filename, secret in (("workload.token", token), ("dispatch.key", key)):
            file = work / filename
            file.write_text(secret.get_secret_value(), encoding="ascii")
            file.chmod(0o600)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work),
               "SAFE_INFER_SOCKET_DIR": str(work),
               "SAFE_INFER_WORKLOAD_TOKEN_FILE": str(work / "workload.token"),
               "SAFE_INFER_DISPATCH_KEY_FILE": str(work / "dispatch.key"),
               "SAFE_INFER_HOST": HOST, "SAFE_INFER_ROUTE": "admission_route",
               "SAFE_INFER_MODEL_REVISION": "offline_v1"}
        path = work / "inference.sock"
        def launch():
            child = subprocess.Popen([sys.executable, "-m", "safe_agent.inference_service"],
                cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            deadline = time.monotonic() + 10
            while True:
                if path.exists():
                    try:
                        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path), retries=0),
                                          base_url="http://" + HOST, timeout=0.2) as health:
                            if health.get("/healthz").status_code == 200:
                                break
                    except httpx.HTTPError:
                        pass
                if child.poll() is not None or time.monotonic() >= deadline:
                    child.kill()
                    child.wait(timeout=3)
                    raise AssertionError("coordinated sidecar launch failed")
                time.sleep(0.02)
            return child
        def stop(child):
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            # Never unlink here: only the lifetime-locked ownership journal may
            # recover a stale socket on the next process launch.
        process = launch()
        url = f"sqlite+pysqlite:///{work / 'state.sqlite'}"
        repo = LedgerRepository.testing_sqlite(url)
        try:
            assert path.is_socket() and path.stat().st_mode & 0o077 == 0
            # A different OS identity cannot even reach the owner-only socket.
            if os.getuid() == 0:
                probe = subprocess.run([sys.executable, "-c",
                    "import socket,sys; s=socket.socket(socket.AF_UNIX); "
                    "\ntry: s.connect(sys.argv[1])\nexcept PermissionError: sys.exit(0)\nelse: sys.exit(1)",
                    str(path)], user=65534, group=65534, extra_groups=[],
                    capture_output=True, timeout=3, check=False, cwd="/")
                assert probe.returncode == 0, "non-owner reached private socket"
            wire = CoordinatedUDSInferenceTransport(path, expected_host=HOST,
                                                    workload_token=token, dispatch_key=key)
            admission = TrustedInferenceAdmission(repo, policy=policy())
            coordinator = TrustedInferenceCoordinator(admission, wire)
            args = assigned(repo, now=datetime.now(UTC))
            result = coordinator.execute(*args)
            assert result.invocation.state == "RESPONSE_VALIDATED", result.invocation.state
            assert result.outcome is not None and result.outcome.provider_effect_performed is False
            repeated = coordinator.execute(*args)
            assert not repeated.dispatched_this_call and repeated.outcome is None
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(path), retries=0),
                              base_url="http://" + HOST, timeout=3) as client:
                headers = {"Authorization": "Bearer " + token.get_secret_value()}
                assert client.post("/v1/infer", headers=headers, json={}).status_code == 404
                assert client.post("/v1/dispatch", json={}).status_code == 401
                assert client.get("/internal/status", headers=headers).json()["accepted"] == 1
            # Inject caller failure AFTER actual sidecar execution, BEFORE SQL finish.
            lost_args = assigned(repo, now=datetime.now(UTC))
            crash = multiprocessing.get_context("spawn").Process(
                target=_send_and_crash, args=(url, lost_args, path, token, key),
            )
            crash.start()
            crash.join(timeout=15)
            if crash.is_alive():
                crash.kill()
                crash.join(timeout=3)
                raise AssertionError("caller crash scenario exceeded its bound")
            assert crash.exitcode == 73, "caller did not reach the post-response crash point"
            repo.close()
            stop(process)
            process = launch()
            # New SQL connection + new service process. Replay cache is now empty,
            # but durable DISPATCH_STARTED must prevent another backend call.
            repo = LedgerRepository.testing_sqlite(url)
            restarted = TrustedInferenceCoordinator(TrustedInferenceAdmission(repo, policy=policy()), wire)
            replayed = restarted.execute(*lost_args)
            assert replayed.invocation.state in ("DISPATCH_STARTED", "UNKNOWN")
            assert replayed.dispatched_this_call is False
            deadline_at = restarted.store.admission.admit(*lost_args).binding.deadline_at
            remaining = (deadline_at - datetime.now(UTC)).total_seconds()
            assert remaining <= 10
            if remaining > 0:
                time.sleep(remaining + 0.02)
            view = restarted.store.recover(*lost_args)
            assert view.state == "UNKNOWN"
            assert restarted.execute(*lost_args).dispatched_this_call is False
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(path), retries=0),
                              base_url="http://" + HOST, timeout=3) as client:
                status = client.get("/internal/status", headers={
                    "Authorization": "Bearer " + token.get_secret_value()}).json()
                assert status["accepted"] == 0, "restart caused a duplicate backend call"
            assert restarted.store.admission.admit(*lost_args).reservation.held_microusd > 0
            print("M7_COORDINATED_UDS_RESTART_NO_REDISPATCH=PASS")
            print("BUSINESS_EFFECTS=0; HOSTED_INFERENCE=DISABLED; M5_LIVE_ACCEPTANCE=BLOCKED")
        finally:
            repo.close()
            if process.poll() is None:
                stop(process)


if __name__ == "__main__":
    main()
