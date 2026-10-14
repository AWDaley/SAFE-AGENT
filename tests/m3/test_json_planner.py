"""No privileged state or authority is injected into configurable text planners."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import ClosedJsonPlanner, select_planner
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.office import AvailableSlot, Projection


def test_model_transport_sees_only_scoped_fields_and_closed_reply():
    start = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    seen: list[str] = []
    context = SafeTaskContext(
        task_id="task_a", organization_id="org_demo", calendar_ref="ref_calendar",
        customer_ref="ref_customer", allowed_start=start,
        allowed_end=start + timedelta(hours=1), duration_minutes=30,
        time_zone="America/New_York", template_id="follow_up_v1",
        simulation_ref="simulation_pending",
    )
    projection = Projection(
        task_id="task_a", organization_id="org_demo", calendar_ref="ref_calendar",
        revision="projection_a", fetched_at=start - timedelta(hours=1),
        expires_at=start + timedelta(minutes=2),
        free_slots=(AvailableSlot(start_at=start, end_at=start + timedelta(hours=1)),),
    )

    def model(data: str) -> str:
        seen.append(data)
        return CalendarCreateEvent(
            calendar_ref=context.calendar_ref, customer_ref=context.customer_ref,
            start_at=start, end_at=start + timedelta(minutes=30),
            time_zone=context.time_zone, template_id=context.template_id,
            simulation_ref=context.simulation_ref, projection_rev=projection.revision,
        ).model_dump_json()

    chosen = select_planner("json", generate=model).propose(context, projection)
    assert isinstance(chosen, CalendarCreateEvent)
    assert len(seen) == 1 and "ref_customer" in seen[0]
    for forbidden in ("customer_name", "email", "oauth", "private_calendar_id", "password"):
        assert forbidden not in seen[0]


@pytest.mark.parametrize("malformed", [
    '[]', '{"operation":"calendar.create_event","secret":"do not pass"}',
    '{"operation":"agent.complete","success":true}',
    '{"operation":"calendar.create_event","operation":"calendar.create_event"}',
])
def test_model_cannot_bypass_closed_proposal(malformed):
    when = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    context = SafeTaskContext(
        task_id="task_a", organization_id="org_demo", calendar_ref="ref_calendar",
        customer_ref="ref_customer", allowed_start=when, allowed_end=when + timedelta(hours=1),
        duration_minutes=30, time_zone="America/New_York", template_id="follow_up_v1",
        simulation_ref="sim_a",
    )
    projection = Projection(
        task_id="task_a", organization_id="org_demo", calendar_ref="ref_calendar",
        revision="proj_a", fetched_at=when - timedelta(hours=1), expires_at=when,
    )
    with pytest.raises(ValueError):
        ClosedJsonPlanner(lambda _: malformed).propose(context, projection)


def test_unconfigured_model_transport_fails_closed():
    with pytest.raises(ValueError):
        select_planner("json")
