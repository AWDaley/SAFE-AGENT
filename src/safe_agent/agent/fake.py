"""Deterministic, network-free planner for synthetic fixture tests."""

from __future__ import annotations

from datetime import timedelta

from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.office import Projection

from .interfaces import SafeTaskContext


class FakePlanner:
    """Choose first fitting free interval; does not mutate or authorize anything."""

    def propose(
        self, context: SafeTaskContext, projection: Projection
    ) -> CalendarCreateEvent | RequestClarification:
        duration = timedelta(minutes=context.duration_minutes)
        for slot in sorted(projection.free_slots, key=lambda entry: entry.start_at):
            start = max(context.allowed_start, slot.start_at)
            end = start + duration
            if end <= min(context.allowed_end, slot.end_at):
                return CalendarCreateEvent(
                    calendar_ref=context.calendar_ref,
                    customer_ref=context.customer_ref,
                    start_at=start,
                    end_at=end,
                    time_zone=context.time_zone,
                    template_id=context.template_id,
                    simulation_ref=context.simulation_ref,
                    projection_rev=projection.revision,
                )
        return RequestClarification(
            reason_code="NO_AVAILABLE_SLOT",
            question="No permitted opening fits the requested window. Choose another time?",
        )
