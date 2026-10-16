"""Explicit local-only maintenance and process commands; no automatic workflow.

The supported target is the hash-locked Linux/CPython 3.12 environment. Roles
share a local owning UID in this development topology, not production isolation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from safe_agent.contracts.inference_coordinator import TrustedDispatchAuthenticator
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.inference_service.service import (
    TrustedInferenceServicePolicy,
    TrustedWorkloadAuthenticator,
    create_inference_service,
)
from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener
from safe_agent.local_office.application import create_local_api, issue_session, require_environment
from safe_agent.local_office.state import HOST, MODEL, ROUTE, initialize, open_office
from safe_agent.local_office.workspace import (
    LocalOfficeBlocked,
    absolute_path,
    load_manifest,
    secret,
    workspace_lock,
)
from safe_agent.tal.reviewed_runtime import require_reviewed_tal_runtime
from safe_agent.tal.trusted_dispatch import TrustedTALOfficeDispatchCheck


def run_sidecar(root: Path) -> None:
    value = load_manifest(root)
    policy = TrustedInferenceServicePolicy(expected_host=HOST, route_id=ROUTE, model_revision=MODEL)
    app = create_inference_service(policy, TrustedWorkloadAuthenticator(
        SecretStr(secret(root, "workload.token", value).decode("ascii"))),
        dispatch_authenticator=TrustedDispatchAuthenticator(
            SecretStr(secret(root, "dispatch.key", value).decode("ascii"))))
    path = root / "inference/inference.sock"
    if len(os.fsencode(path)) > 103:
        raise LocalOfficeBlocked("WORKSPACE_SOCKET_PATH_TOO_LONG")
    import uvicorn

    with OwnedUnixListener(path) as listener:
        uvicorn.run(app, fd=listener.fileno(), access_log=False, proxy_headers=False,
                    server_header=False, date_header=False, log_level="warning", lifespan="off")


def run_worker(root: Path, *, passes: int = 1, interval: float = 1.0) -> dict[str, object]:
    if not 1 <= passes <= 1000 or not 0.1 <= interval <= 60:
        raise LocalOfficeBlocked("WORKER_BOUNDS_DENIED")
    require_reviewed_tal_runtime()
    processed, committed = 0, 0
    with workspace_lock(root, "worker.lock"):
        for index in range(passes):
            with workspace_lock(root):
                local = open_office(root)
                try:
                    worker = SyntheticRecoveryWorker(local.repo, local.broker, local.calendar,
                        office=local.office, tal_dispatch=TrustedTALOfficeDispatchCheck())
                    receipt = worker.process_one(now=datetime.now(UTC))
                    if receipt is not None:
                        processed += 1
                        committed += int(receipt.outcome == "COMMITTED")
                finally:
                    local.close()
            if index + 1 < passes:
                time.sleep(interval)
    return {"mode": "PERSISTENT_SYNTHETIC_ONLY", "claims_processed": processed,
            "committed_receipts": committed, "live_providers_enabled": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE-AGENT persistent synthetic local office")
    parser.add_argument("--workspace", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize a NEW workspace with existing migrations")
    init.add_argument("--migrations", required=True, type=Path,
                      help="Trusted checkout's migrations directory; used only during init")
    commands.add_parser("status", help="Inspect local persisted state without starting services")
    session = commands.add_parser("session", help="Explicitly issue a short-lived local token")
    session.add_argument("--role", required=True, choices=("requester", "approver"))
    session.add_argument("--seconds", type=int, default=900)
    serve = commands.add_parser("serve", help="Start loopback-only operator API, without a worker")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("sidecar", help="Start the offline mock over its private Unix socket")
    worker = commands.add_parser("worker", help="Run bounded synthetic execution passes separately")
    worker.add_argument("--passes", type=int, default=1)
    worker.add_argument("--interval", type=float, default=1.0)
    options = parser.parse_args()
    try:
        require_environment()
        root = absolute_path(options.workspace)
        os.umask(0o077)
        if len(os.fsencode(root / "inference/inference.sock")) > 103:
            raise LocalOfficeBlocked("WORKSPACE_SOCKET_PATH_TOO_LONG")
        if options.command == "init":
            value = initialize(root, options.migrations)
            report = {"status": "INITIALIZED", "workspace_id": value.workspace_id,
                      "mode": value.mode, "live_providers_enabled": False}
        elif options.command == "session":
            print(issue_session(root, options.role, lifetime=options.seconds))
            return
        elif options.command == "serve":
            import uvicorn

            with workspace_lock(root, "api.lock"):
                app = create_local_api(root, port=options.port)
                try:
                    uvicorn.run(app, host="127.0.0.1", port=options.port,
                        access_log=False, proxy_headers=False, server_header=False,
                        date_header=False, log_level="warning", lifespan="on",
                        limit_concurrency=16, timeout_keep_alive=5)
                finally:
                    app.state.local_office.close()
            return
        elif options.command == "sidecar":
            run_sidecar(root)
            return
        elif options.command == "worker":
            report = run_worker(root, passes=options.passes, interval=options.interval)
        else:
            with workspace_lock(root):
                local = open_office(root)
                try:
                    report = {"status": "PERSISTED_STATE_AVAILABLE",
                        "workspace_id": local.manifest.workspace_id,
                        "mode": "PERSISTENT_SYNTHETIC_ONLY", "live_providers_enabled": False,
                        "business_execution_authorized": False, "production_authorized": False}
                finally:
                    local.close()
        print(json.dumps(report, sort_keys=True))
    except KeyboardInterrupt:
        return
    except Exception as error:
        # Explicit session output is the only command that prints a token.
        # Do not print raw database URLs, paths, provider errors, or key material.
        code = str(error) if isinstance(error, LocalOfficeBlocked) else "LOCAL_STARTUP_BLOCKED"
        print(json.dumps({"status": "BLOCKED", "reason": code,
                          "live_providers_enabled": False}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
