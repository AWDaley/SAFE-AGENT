"""Authenticated recovery routes, enabled only with coordinated planning."""
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from safe_agent.contracts.planning_recovery import (
    PlanningRecoveryView,
    RetireTaskCommand,
    ResumeSavedResultCommand,
)
from safe_agent.operator_ui.http import ASSET_DIR, SECURE_HEADERS
from safe_agent.orchestration.coordinated_planning import TrustedCoordinatedPlanning
from safe_agent.orchestration.planning_recovery import PlanningRecoveryService
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.repository import MissingTask


def create_planning_recovery_router(
    planning: TrustedCoordinatedPlanning,
    operator_dependency: Callable[..., Principal],
    *, enable_ui: bool = False,
) -> APIRouter:
    router = APIRouter()
    recovery = PlanningRecoveryService(planning)

    @router.get("/v1/tasks/{task_id}/planning/recovery")
    def inspect_recovery(
        task_id: str, principal: Annotated[Principal, Depends(operator_dependency)],
    ) -> PlanningRecoveryView:
        try:
            return recovery.inspect(principal, task_id)
        except (AccessDenied, MissingTask):
            raise HTTPException(status_code=404, detail="task unavailable") from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None
        except (ValueError, TypeError):
            raise HTTPException(status_code=409, detail="recovery state unavailable") from None
        except Exception:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None

    @router.post("/v1/tasks/{task_id}/planning/resume-saved")
    def resume_saved(
        task_id: str, body: ResumeSavedResultCommand,
        principal: Annotated[Principal, Depends(operator_dependency)],
    ) -> PlanningRecoveryView:
        try:
            return recovery.resume(principal, task_id, body)
        except (AccessDenied, MissingTask):
            raise HTTPException(status_code=404, detail="task unavailable") from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None
        except (ValueError, TypeError):
            raise HTTPException(status_code=409, detail="refresh recovery state before continuing") from None
        except Exception:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None

    @router.post("/v1/tasks/{task_id}/planning/retire")
    def retire_task(
        task_id: str, body: RetireTaskCommand,
        principal: Annotated[Principal, Depends(operator_dependency)],
    ) -> PlanningRecoveryView:
        try:
            return recovery.retire(principal, task_id, body)
        except (AccessDenied, MissingTask):
            raise HTTPException(status_code=404, detail="task unavailable") from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None
        except (ValueError, TypeError):
            raise HTTPException(status_code=409, detail="refresh recovery state before continuing") from None
        except Exception:
            raise HTTPException(status_code=503, detail="recovery service unavailable") from None

    if enable_ui:
        @router.get("/operator/recovery", include_in_schema=False)
        def home() -> HTMLResponse:
            return HTMLResponse(ASSET_DIR.joinpath("recovery.html").read_text(encoding="utf-8"),
                                headers=SECURE_HEADERS)

        @router.get("/operator/assets/recovery.js", include_in_schema=False)
        def script() -> Response:
            return Response(ASSET_DIR.joinpath("recovery.js").read_text(encoding="utf-8"),
                            media_type="text/javascript", headers=SECURE_HEADERS)

    return router
