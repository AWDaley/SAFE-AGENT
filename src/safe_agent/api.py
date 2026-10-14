"""Restricted M2 operator API: bearer-authenticated status, evidence and approval.

No proposal dispatch, task-grant creation, secret resolution, or raw customer record
endpoints. Task onboarding remains a privileged internal provisioning step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from safe_agent.evidence.ledger import LedgerEvent, TaskSnapshot
from safe_agent.orchestration.office_tasks import ApprovalInput, TrustedOfficeTasks
from safe_agent.security.principal import AccessDenied, Principal, SignedOperatorTokens
from safe_agent.storage.repository import MissingTask, StateConflict


def create_app(coordinator: TrustedOfficeTasks, authenticator: SignedOperatorTokens,
               *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI:
    app = FastAPI(title="SAFE-AGENT M2 Local Operator", version="0.2.0")
    bearer = HTTPBearer(auto_error=False)

    def operator(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return authenticator.verify(credentials.credentials, now=clock())
        except AccessDenied as error:
            raise HTTPException(status_code=401, detail="invalid operator authorization") from error

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "M2_NO_PROVIDER_EXECUTION"}

    @app.get("/v1/tasks/{task_id}")
    def inspect(task_id: str, principal: Principal = Depends(operator)) -> TaskSnapshot:
        try:
            return coordinator.inspect(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.get("/v1/tasks/{task_id}/events")
    def events(task_id: str, principal: Principal = Depends(operator)) -> tuple[LedgerEvent, ...]:
        try:
            return coordinator.events(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.get("/v1/tasks/{task_id}/replay")
    def replay(task_id: str, principal: Principal = Depends(operator)) -> TaskSnapshot:
        try:
            return coordinator.replay(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.post("/v1/tasks/{task_id}/approvals")
    def approve(task_id: str, body: ApprovalInput,
                principal: Principal = Depends(operator)) -> dict[str, str | int]:
        try:
            decision = coordinator.approve(principal, task_id, body, now=clock())
            return {"decision": decision.decision.value,
                    "proposal_hash": decision.proposal_hash,
                    "reason_code": decision.reason_code,
                    "state_version": decision.state_version,
                    "warning": "NO_PROVIDER_EFFECT_OR_UPSTREAM_TAL_AUTHORIZATION"}
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error
        except StateConflict as error:
            raise HTTPException(status_code=409, detail="approval state mismatch") from error

    return app
