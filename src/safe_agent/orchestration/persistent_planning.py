"""Opt-in durable, text-only offline planning over a scoped synthetic workspace.

Model callbacks receive exactly the existing minimized M0 context and
free/busy projection, never secrets, raw customer data, a resolver or tools.
An admitted proposal still needs the existing trusted simulation and TAL-style
gate, separate human approval and independently verified synthetic worker.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from safe_agent.agent.fake import FakePlanner
from safe_agent.agent.json_planner import ClosedJsonPlanner
from safe_agent.broker.references import ReferenceDenied
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.planning import (
    PlanningCommand,
    PlanningMode,
    PlanningReason,
    PlanningReceipt,
    PlanningWorkspaceView,
)
from safe_agent.orchestration.synthetic_workflow import (
    NoPermittedSyntheticSlot,
    TrustedSyntheticWorkflow,
)
from safe_agent.security.principal import Principal
from safe_agent.storage.planning import PlanningWorkspaceStore
from safe_agent.storage.repository import StateConflict
from safe_agent.virtual_office.service import StaleProjection, UnissuedProjection


class TrustedPersistentPlanning:
    """Bounded orchestration: durable claim -> sanitized plan -> safe outcome.

    Never permits the browser to supply planner mode, extra permissions,
    instructions, raw LLM responses or scope updates. Model generator callbacks
    must be provided by trusted bootstrap code and enforce their own transport
    timeout and cost quotas; process sandboxing remains a deployment blocker.
    """

    def __init__(
        self, store: PlanningWorkspaceStore, workflow: TrustedSyntheticWorkflow,
        *,
        planner: FakePlanner | ClosedJsonPlanner | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        selected = planner or FakePlanner()
        if type(selected) not in (FakePlanner, ClosedJsonPlanner):
            raise TypeError("only closed text planners or synthetic fake planner supported")
        self._store = store
        self._workflow = workflow
        self._planner = selected
        self._mode: PlanningMode = (
            "text_only" if type(selected) is ClosedJsonPlanner else "deterministic"
        )
        self._clock = clock

    def inspect(
        self, principal: Principal, task_id: str, *, now: datetime,
    ) -> PlanningWorkspaceView:
        return self._store.view(
            principal, task_id, now=require_aware(now), mode=self._mode,
        )

    @staticmethod
    def _receipt(view: PlanningWorkspaceView) -> PlanningReceipt:
        if view.attempts_used < 1:
            raise StateConflict("there has been no bounded planning attempt")
        if view.status not in ("PREPARED", "NEEDS_CLARIFICATION", "READY", "EXHAUSTED"):
            raise StateConflict("planning lease remains pending")
        return PlanningReceipt(
            task_id=view.task_id, status=view.status,
            workspace_version=view.workspace_version,
            attempts_used=view.attempts_used, max_attempts=view.max_attempts,
            reason_code=view.last_reason, proposal_hash=view.proposal_hash,
        )

    def plan(
        self, principal: Principal, task_id: str, body: PlanningCommand, *,
        now: datetime,
    ) -> PlanningReceipt:
        now = require_aware(now)
        # Claim a bounded attempt BEFORE invoking an injected model callback.
        lease = self._store.begin(
            principal, task_id,
            expected_task_version=body.expected_state_version,
            expected_workspace_version=body.expected_workspace_version,
            mode=self._mode, now=now,
        )
        if lease is None:
            return self._receipt(self.inspect(principal, task_id, now=now))
        try:
            self._workflow.prepare(
                principal, task_id,
                expected_state_version=body.expected_state_version,
                now=now,
                planner_override=self._planner, planning_lease=lease,
                completion_clock=self._clock,
            )
        except NoPermittedSyntheticSlot as error:
            # Never return or persist free-form model questions.
            reason: PlanningReason = (
                str(error) if str(error) in ("NO_AVAILABLE_SLOT", "INSUFFICIENT_CONTEXT")
                else "NO_AVAILABLE_SLOT"
            )  # type: ignore[assignment]
            self._store.fail(
                lease, result_kind="NEEDS_CLARIFICATION",
                reason_code=reason, now=self._clock(),
            )
        except (StateConflict, StaleProjection, UnissuedProjection, ReferenceDenied):
            self._store.fail(
                lease, result_kind="RETRYABLE_FAILURE",
                reason_code="STALE_OR_DENIED", now=self._clock(),
            )
        except Exception:
            # The model callback may fail, time out or return malicious text.
            # No raw output/exception is logged, persisted, or exposed.
            self._store.fail(
                lease, result_kind="RETRYABLE_FAILURE",
                reason_code="MODEL_RESPONSE_REJECTED", now=self._clock(),
            )
        return self._receipt(self.inspect(principal, task_id, now=self._clock()))
