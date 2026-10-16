"""Coordinated application queue entrypoint; legacy fixture tasks are not adopted."""
from __future__ import annotations

from datetime import datetime

from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.orchestration.coordinated_planning import TrustedCoordinatedPlanning
from safe_agent.orchestration.synthetic_workflow import SyntheticQueueReceipt
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.coordinated_execution import coordinated_execution_input
from safe_agent.storage.repository import StateConflict


def queue_coordinated_task(
    planning: TrustedCoordinatedPlanning, principal: Principal, task_id: str, *,
    expected_state_version: int, now: datetime,
) -> SyntheticQueueReceipt:
    """Require durable coordinated provenance before using the existing queue.

    The gateway repeats provenance, current state, approval, and source checks in
    its own atomic intent transaction. This preliminary read grants no authority.
    """
    if type(planning) is not TrustedCoordinatedPlanning:
        raise TypeError("exact coordinated planning required")
    now = require_aware(now)
    with planning._store._repo.write_task(task_id, serialize_sqlite=True) as tx:
        principal.require("task:execute", tx.task.organization_id, now)
        if principal.actor_id != tx.task.initiator_id:
            raise AccessDenied("task initiator required")
        if tx.snapshot.state_version != expected_state_version:
            raise StateConflict("COORDINATED_QUEUE_VERSION_DENIED")
        proposal = tx.read_active_proposal()
        if not isinstance(proposal, CalendarCreateEvent):
            raise StateConflict("COORDINATED_CALENDAR_REQUIRED")
        projection, _ = tx.preview(proposal.simulation_ref)
        if coordinated_execution_input(tx, proposal, projection) is None:
            raise StateConflict("COORDINATED_PLANNING_REQUIRED")
    return planning._workflow.queue(
        principal, task_id, expected_state_version=expected_state_version, now=now,
    )
