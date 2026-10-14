"""Authenticated operator API with opt-in trusted synthetic task assignment.

No model-facing grant creation, direct provider dispatch, secret resolution or raw
customer record endpoints. The trusted provisioner compiles immutable authority.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import RequestResponseEndpoint

from safe_agent.broker.references import ReferenceDenied
from safe_agent.contracts.intake import TaskIntakeReceipt, TaskIntakeRequest, TaskOptionsPage
from safe_agent.contracts.operator import OperatorInboxPage, OperatorProposalPreview
from safe_agent.evidence.ledger import LedgerEvent, TaskSnapshot
from safe_agent.orchestration.office_tasks import ApprovalInput, TrustedOfficeTasks
from safe_agent.orchestration.synthetic_workflow import (
    NoPermittedSyntheticSlot,
    SyntheticPreparationReceipt,
    SyntheticQueueReceipt,
    TrustedSyntheticWorkflow,
    WorkflowVersionCommand,
)
from safe_agent.orchestration.task_provisioning import (
    InvalidTaskWindow,
    OptionUnavailable,
    TrustedTaskProvisioner,
)
from safe_agent.security.principal import AccessDenied, Principal, SignedOperatorTokens
from safe_agent.storage.repository import MissingTask, StateConflict
from safe_agent.virtual_office.service import StaleProjection, UnissuedProjection

if TYPE_CHECKING:
    from safe_agent.integrations.google_calendar.oauth import GoogleOAuthService


def create_app(coordinator: TrustedOfficeTasks, authenticator: SignedOperatorTokens,
               *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
               google_test_oauth: "GoogleOAuthService | None" = None,
               enable_operator_ui: bool = False,
               task_provisioner: TrustedTaskProvisioner | None = None,
               synthetic_workflow: TrustedSyntheticWorkflow | None = None) -> FastAPI:
    app = FastAPI(title="SAFE-AGENT M2 Local Operator", version="0.2.0")
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def no_store_task_data(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Never cache operator task data or local UI assets at any proxy."""
        response = await call_next(request)
        if (request.url.path.startswith("/v1/tasks")
                or request.url.path.startswith("/v1/task-options")
                or request.url.path.startswith("/operator")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Vary"] = "Authorization"
        return response

    def operator(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return authenticator.verify(credentials.credentials, now=clock())
        except AccessDenied as error:
            raise HTTPException(status_code=401, detail="invalid operator authorization") from error

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "M2_NO_PROVIDER_EXECUTION"}

    if task_provisioner is not None:
        @app.get("/v1/task-options")
        def task_options(
            principal: Annotated[Principal, Depends(operator)],
        ) -> TaskOptionsPage:
            try:
                return task_provisioner.available_options(principal, now=clock())
            except AccessDenied as error:
                raise HTTPException(status_code=403, detail="operator scope denied") from error

        @app.post("/v1/tasks", status_code=201)
        def assign_task(
            body: TaskIntakeRequest,
            principal: Annotated[Principal, Depends(operator)],
        ) -> TaskIntakeReceipt:
            try:
                return task_provisioner.create(principal, body, now=clock())
            except AccessDenied as error:
                raise HTTPException(status_code=403, detail="operator scope denied") from error
            except OptionUnavailable as error:
                raise HTTPException(status_code=404, detail="task option unavailable") from error
            except InvalidTaskWindow as error:
                raise HTTPException(status_code=422, detail="invalid task window") from error
            except (StateConflict, IntegrityError) as error:
                raise HTTPException(status_code=409, detail="task intake conflict") from error

    @app.get("/v1/tasks")
    def inbox(
        principal: Annotated[Principal, Depends(operator)],
        limit: int = Query(default=20, ge=1, le=50),
        after: str | None = Query(
            default=None, max_length=128,
            pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$",
        ),
    ) -> OperatorInboxPage:
        """Opaque, bounded, tenant-only task cards. No task discovery outside org."""
        try:
            return coordinator.operator_inbox(
                principal, now=clock(), after=after, limit=limit,
            )
        except AccessDenied as error:
            raise HTTPException(status_code=403, detail="operator scope denied") from error
        except StateConflict as error:
            raise HTTPException(status_code=409, detail="task inbox integrity failure") from error

    if synthetic_workflow is not None:
        @app.post("/v1/tasks/{task_id}/prepare")
        def prepare_task(
            task_id: str, body: WorkflowVersionCommand,
            principal: Annotated[Principal, Depends(operator)],
        ) -> SyntheticPreparationReceipt:
            """Only assigned synthetic tasks; no provider dispatch from HTTP."""
            try:
                return synthetic_workflow.prepare(
                    principal, task_id,
                    expected_state_version=body.expected_state_version, now=clock(),
                )
            except (AccessDenied, MissingTask) as error:
                raise HTTPException(status_code=404, detail="task unavailable") from error
            except NoPermittedSyntheticSlot as error:
                raise HTTPException(
                    status_code=409, detail="no permitted synthetic option available",
                ) from error
            except (StateConflict, ReferenceDenied, StaleProjection,
                    UnissuedProjection, ValueError) as error:
                raise HTTPException(
                    status_code=409, detail="synthetic preparation unavailable",
                ) from error

        @app.post("/v1/tasks/{task_id}/queue-synthetic")
        def queue_synthetic(
            task_id: str, body: WorkflowVersionCommand,
            principal: Annotated[Principal, Depends(operator)],
        ) -> SyntheticQueueReceipt:
            """Stage only; a separate privileged recovery worker performs effects."""
            try:
                return synthetic_workflow.queue(
                    principal, task_id,
                    expected_state_version=body.expected_state_version, now=clock(),
                )
            except (AccessDenied, MissingTask) as error:
                raise HTTPException(status_code=404, detail="task unavailable") from error
            except (StateConflict, ReferenceDenied, ValueError) as error:
                raise HTTPException(
                    status_code=409, detail="synthetic queue unavailable",
                ) from error

    @app.get("/v1/tasks/{task_id}/preview")
    def review_preview(
        task_id: str, principal: Annotated[Principal, Depends(operator)],
    ) -> OperatorProposalPreview:
        """Only pinned and previously simulated proposal details; advisory."""
        try:
            return coordinator.operator_proposal_preview(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error
        except (StateConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail="proposal preview unavailable") from error

    @app.get("/v1/tasks/{task_id}")
    def inspect(task_id: str, principal: Annotated[Principal, Depends(operator)]) -> TaskSnapshot:
        try:
            return coordinator.inspect(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.get("/v1/tasks/{task_id}/execution")
    def execution_status(
        task_id: str, principal: Annotated[Principal, Depends(operator)],
    ) -> dict[str, object]:
        """Safe operator outcome: queued/uncertain/review/verified, never raw bindings."""
        try:
            return coordinator.execution_status(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.get("/v1/tasks/{task_id}/events")
    def events(task_id: str, principal: Annotated[Principal, Depends(operator)]) -> tuple[LedgerEvent, ...]:
        try:
            return coordinator.events(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.get("/v1/tasks/{task_id}/replay")
    def replay(task_id: str, principal: Annotated[Principal, Depends(operator)]) -> TaskSnapshot:
        try:
            return coordinator.replay(principal, task_id, now=clock())
        except (AccessDenied, MissingTask) as error:
            raise HTTPException(status_code=404, detail="task unavailable") from error

    @app.post("/v1/tasks/{task_id}/approvals")
    def approve(task_id: str, body: ApprovalInput,
                principal: Annotated[Principal, Depends(operator)]) -> dict[str, str | int]:
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

    if enable_operator_ui:
        from safe_agent.operator_ui.http import create_operator_ui_router

        app.include_router(create_operator_ui_router())

    if google_test_oauth is not None:
        from safe_agent.integrations.google_calendar.http import create_google_test_router

        app.include_router(create_google_test_router(
            google_test_oauth, authenticator, clock=clock,
        ))
    return app
