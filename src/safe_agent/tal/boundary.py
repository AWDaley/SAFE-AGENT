"""Explicit fail-closed placeholder for upstream TAL's trusted action gate.

The upstream 0.3 invoice-domain runtime cannot authorize calendar.create_event.
This deliberately provides NO execution path or alternate policy engine.
"""

from __future__ import annotations

from typing import Protocol

from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import ActionDecision, DecisionStatus, Task


class TALAuthority(Protocol):
    """Trusted service boundary; model-side modules must never import this."""

    def validate(self, task: Task, proposal: CalendarCreateEvent) -> ActionDecision: ...


class UnconfiguredTALAuthority:
    def validate(self, task: Task, proposal: CalendarCreateEvent) -> ActionDecision:
        from safe_agent.canonical import sha256

        return ActionDecision(
            decision=DecisionStatus.DENIED,
            manifest_hash=task.manifest_hash,
            proposal_hash=sha256(proposal),
            reason_code="TAL_OFFICE_DOMAIN_NOT_INTEGRATED",
            state_version=0,
        )
