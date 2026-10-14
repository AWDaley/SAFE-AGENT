"""M0 planner-only shell. It never authorizes, dispatches or completes a task."""

from __future__ import annotations

from typing import Literal

from pydantic import TypeAdapter

from safe_agent.agent.interfaces import Planner, SafeTaskContext
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent, Proposal, RequestClarification
from safe_agent.contracts.base import Digest, Identifier, StrictModel
from safe_agent.contracts.control import Task, TaskState
from safe_agent.contracts.office import Projection


class PlanningResult(StrictModel):
    task_id: Identifier
    state: Literal[TaskState.PROPOSED_UNVERIFIED] = TaskState.PROPOSED_UNVERIFIED
    proposal_hash: Digest
    proposal: CalendarCreateEvent | RequestClarification
    explicitly_not_committed: Literal[True] = True


class TaskRunner:
    """Pure planning shell; a trusted TAL gateway will be added after M0."""

    def __init__(self, planner: Planner) -> None:
        self._planner = planner

    def plan_once(
        self, task: Task, context: SafeTaskContext, projection: Projection
    ) -> PlanningResult:
        if task.state not in {TaskState.ASSIGNED, TaskState.PLANNING}:
            raise ValueError("task is not in a plannable state")
        if task.task_id != context.task_id or task.task_id != projection.task_id:
            raise ValueError("cross-task context/projection rejected")
        if task.organization_id != context.organization_id:
            raise ValueError("cross-organization context rejected")
        if task.organization_id != projection.organization_id:
            raise ValueError("cross-organization projection rejected")
        if context.calendar_ref != projection.calendar_ref:
            raise ValueError("calendar scope mismatch")
        proposal: CalendarCreateEvent | RequestClarification = TypeAdapter(Proposal).validate_python(
            self._planner.propose(context, projection)
        )
        if isinstance(proposal, CalendarCreateEvent):
            if proposal.calendar_ref != context.calendar_ref:
                raise ValueError("proposal calendar mismatch")
            if proposal.customer_ref != context.customer_ref:
                raise ValueError("proposal customer mismatch")
            if proposal.projection_rev != projection.revision:
                raise ValueError("proposal projection revision mismatch")
        return PlanningResult(
            task_id=task.task_id, proposal=proposal,
            proposal_hash=sha256(proposal),
        )
