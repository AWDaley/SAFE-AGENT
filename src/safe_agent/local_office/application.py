"""Trusted local application assembly using real coordinator and recovery paths.

One API process and one optional worker cooperate via an owner-only workspace
lock. The browser never starts a worker, supplies provider config, or mints tokens.
"""
from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from pydantic import SecretStr
from starlette.responses import JSONResponse, RedirectResponse

from safe_agent.api import create_app
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.local_office.state import (
    HOST,
    ORGANIZATION,
    LocalOffice,
    check_database,
    local_policy,
    open_office,
)
from safe_agent.local_office.workspace import (
    LocalOfficeBlocked,
    absolute_path,
    load_manifest,
    secret,
    workspace_lock,
)
from safe_agent.orchestration.coordinated_planning import create_coordinated_planning
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.orchestration.task_provisioning import (
    TrustedSyntheticOption,
    TrustedTaskProvisioner,
)
from safe_agent.runtime.preflight import inspect_environment
from safe_agent.security.principal import AccessDenied, Principal, SignedOperatorTokens
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.planning import PlanningWorkspaceStore
from safe_agent.storage.repository import MissingTask
from safe_agent.tal.office_gate import OfficeGate

ROLE = Literal["requester", "approver"]


def require_environment() -> None:
    if inspect_environment().status != "PASS":
        raise LocalOfficeBlocked("LOCKED_ENVIRONMENT_REQUIRED_RUN_DOCTOR")


def principal_for(role: ROLE, *, now: datetime, lifetime: int = 900) -> Principal:
    if role not in {"requester", "approver"} or not 60 <= lifetime <= 3600:
        raise LocalOfficeBlocked("LOCAL_SESSION_POLICY_DENIED")
    return Principal(
        organization_id=ORGANIZATION, actor_id="local_" + role,
        scopes=(("task:create", "task:prepare", "task:propose", "task:execute",
                 "task:read", "task:audit") if role == "requester"
                else ("task:approve", "task:read", "task:audit")),
        authenticated_until=now + timedelta(seconds=lifetime),
    )


def issue_session(root: Path, role: ROLE, *, lifetime: int = 900) -> str:
    root = absolute_path(root)
    value = load_manifest(root)
    return SignedOperatorTokens(secret(root, "operator.key", value)).issue_for_trusted_operator(
        principal_for(role, now=datetime.now(UTC), lifetime=lifetime),
    )


def sidecar_available(root: Path) -> bool:
    path = root / "inference/inference.sock"
    try:
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or path.resolve(strict=True) != path):
            return False
        # Readiness only, never inference admission. No fallback to TCP or DNS.
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path), retries=0),
                          base_url="http://" + HOST, timeout=0.25,
                          trust_env=False, follow_redirects=False) as client:
            return client.get("/healthz").status_code == 200
    except (OSError, ValueError, httpx.HTTPError):
        return False


def _load_bindings(office: LocalOffice, principal: Principal, path: str) -> None:
    match = re.fullmatch(r"/v1/tasks/([A-Za-z][A-Za-z0-9._:-]{0,127})(?:/[^/]*)?", path)
    if match is None:
        return
    task_id = match.group(1)
    record = office.repo.read_task_record(task_id)
    principal.require("task:read", record.organization_id, datetime.now(UTC))
    # Reload revocation-aware persisted references, never restore a stale broker
    # export from a startup file. The broker validates the original HMAC records.
    office.broker.trusted_import(office.repo.load_protected_references(task_id, record.organization_id))


def create_local_api(root: Path, *, port: int = 8765) -> FastAPI:
    require_environment()
    if not 1024 <= port <= 65535:
        raise LocalOfficeBlocked("LOCAL_PORT_DENIED")
    root = absolute_path(root)
    with workspace_lock(root):
        local = open_office(root)
    try:
        tokens = SignedOperatorTokens(secret(root, "operator.key", local.manifest))
        wire = CoordinatedUDSInferenceTransport(root / "inference/inference.sock",
            expected_host=HOST,
            workload_token=SecretStr(secret(root, "workload.token", local.manifest).decode("ascii")),
            dispatch_key=SecretStr(secret(root, "dispatch.key", local.manifest).decode("ascii")))
        gateway = SyntheticExecutionGateway(local.repo, local.office, OfficeGate(local.broker),
                                            local.broker, local.calendar)
        workflow = TrustedSyntheticWorkflow(local.repo, local.office, local.broker,
                                             local.coordinator, gateway=gateway)
        planning = create_coordinated_planning(
            PlanningWorkspaceStore(local.repo, max_attempts=1), workflow,
            admission=TrustedInferenceAdmission(local.repo, policy=local_policy()), transport=wire,
        )
        # One proposal consumes a policy cycle. The pinned TAL dispatcher needs
        # remaining cycle capacity to check its execution. This is NOT a second
        # inference attempt: max_attempts stays one and max_external_effects one.
        option = TrustedSyntheticOption(
            option_id="local_follow_up", organization_id=ORGANIZATION,
            initiator_id="local_requester", workflow="schedule_follow_up",
            customer_resource_id="cust_c1", calendar_resource_id="cal_main_private",
            appointment_template_id="follow_up_v1", time_zone="America/New_York",
            requires_approval=True, max_cycles=2,
        )
        provisioner = TrustedTaskProvisioner(local.coordinator, local.repo, local.broker,
                                             local.provider.snapshot(ORGANIZATION), (option,))
        app = create_app(local.coordinator, tokens, enable_operator_ui=True,
                         task_provisioner=provisioner, synthetic_workflow=workflow,
                         persistent_planning=planning)
        # Trusted process lifecycle only; never returned in an HTTP response.
        app.state.local_office = local
        app.add_event_handler("shutdown", local.close)
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

        @app.middleware("http")
        async def local_boundary(request: Request, call_next):
            hosts = request.headers.getlist("host")
            origins = request.headers.getlist("origin")
            if len(hosts) != 1 or hosts[0] not in allowed_hosts:
                return JSONResponse({"detail": "local host required"}, status_code=400)
            if len(origins) > 1 or (origins and origins[0] != "http://" + hosts[0]):
                return JSONResponse({"detail": "local origin required"}, status_code=403)
            if not request.url.path.startswith("/v1/"):
                return await call_next(request)
            try:
                authorization = request.headers.get("authorization", "")
                if not authorization.startswith("Bearer "):
                    raise AccessDenied("authorization required")
                actor = tokens.verify(authorization[7:], now=datetime.now(UTC))
                if (actor.organization_id != ORGANIZATION
                        or actor.actor_id not in {"local_requester", "local_approver"}):
                    raise AccessDenied("local principal required")
                with workspace_lock(root):
                    if load_manifest(root) != local.manifest:
                        raise LocalOfficeBlocked("WORKSPACE_CHANGED_RESTART_REQUIRED")
                    local.refresh()
                    _load_bindings(local, actor, request.url.path)
                    return await call_next(request)
            except AccessDenied:
                return JSONResponse({"detail": "local authorization denied"}, status_code=401)
            except MissingTask:
                return JSONResponse({"detail": "task unavailable"}, status_code=404)
            except Exception:
                # Neither filesystem locations nor raw SQLite/provider exceptions
                # are reflected to a browser. Never retry an effect in middleware.
                return JSONResponse({"detail": "local workspace unavailable"}, status_code=503,
                                    headers={"Cache-Control": "no-store"})

        @app.get("/")
        def home():
            return RedirectResponse("/operator", status_code=307)

        @app.get("/healthz")
        def liveness():
            return {"status": "RUNNING", "mode": "PERSISTENT_SYNTHETIC_ONLY",
                    "live_providers_enabled": False}

        @app.get("/readyz")
        def readiness():
            available = False
            try:
                with workspace_lock(root):
                    if load_manifest(root) != local.manifest:
                        raise LocalOfficeBlocked("WORKSPACE_CHANGED")
                    check_database(root)
                    local.provider.snapshot(ORGANIZATION)
                    available = sidecar_available(root)
            except Exception:
                pass
            return JSONResponse({"status": "READY" if available else "NOT_READY",
                "scope": "API_AND_OFFLINE_SIDECAR_ONLY", "separate_worker_required": True,
                "business_execution_authorized": False, "production_authorized": False},
                status_code=200 if available else 503, headers={"Cache-Control": "no-store"})
        return app
    except BaseException:
        local.close()
        raise
