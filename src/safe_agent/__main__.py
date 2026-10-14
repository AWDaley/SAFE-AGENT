"""Offline demonstrations only: model proposals and simulations never commit."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.contracts import CalendarCreateEvent, Projection, SimulatedChange, Task
from safe_agent.orchestration import TaskRunner
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar


def _fixtures() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures/synthetic_office"


def _m0_demo() -> None:
    fixture = _fixtures() / "demo.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    result = TaskRunner(FakePlanner()).plan_once(
        Task.model_validate_json(json.dumps(data["task"])),
        SafeTaskContext.model_validate_json(json.dumps(data["context"])),
        Projection.model_validate_json(json.dumps(data["projection"])),
    )
    print(result.model_dump_json(indent=2))
    print("M0 ONLY: proposal not authorized, not executed, and not verified.")


def _m1_demo() -> None:
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    # This key and all customer records are solely synthetic test inputs;
    # real credentials or resource IDs must not enter model-visible output.
    broker = ReferenceBroker(secret=b"SAFE_AGENT_M1_SYNTHETIC_DEMO_KEY_NOT_FOR_PRODUCTION_123")
    scope = BrokerScope(organization_id="org_demo", initiator_id="employee_demo",
                        task_id="task_demo", operation="calendar.create_event", now=now)
    calendar_ref = broker.mint(
        resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
        scope=scope, permitted_operations=("calendar.create_event",),
        expires_at=now + timedelta(days=2),
    )
    customer_ref = broker.mint(
        resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("calendar.create_event", "tasks.create"),
        expires_at=now + timedelta(days=2),
    )
    office = VirtualOffice(SyntheticOfficeState.from_fixture(_fixtures() / "office_state.json"),
                           broker)
    start = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    end = datetime.fromisoformat("2026-10-15T17:00:00-04:00")
    projection = office.observe(ObserveCalendar(
        organization_id="org_demo", initiator_id="employee_demo", task_id="task_demo",
        calendar_ref=calendar_ref, customer_ref=customer_ref,
        from_at=start, until_at=end,
    ), as_of=now)
    context = SafeTaskContext(
        task_id="task_demo", organization_id="org_demo",
        calendar_ref=calendar_ref, customer_ref=customer_ref,
        allowed_start=start, allowed_end=end, duration_minutes=30,
        time_zone="America/New_York", template_id="follow_up_v1",
        simulation_ref="simulation_not_bound_until_m2",
    )
    demo = json.loads((_fixtures() / "demo.json").read_text(encoding="utf-8"))
    planned = TaskRunner(FakePlanner()).plan_once(
        Task.model_validate_json(json.dumps(demo["task"])), context,
        projection.to_planner_projection(),
    )
    if not isinstance(planned.proposal, CalendarCreateEvent):
        raise ValueError("synthetic M1 fixture has no available meeting")
    proposal = planned.proposal
    preview = office.simulate(
        projection,
        SimulatedChange(calendar_ref=proposal.calendar_ref,
                        customer_ref=proposal.customer_ref,
                        start_at=proposal.start_at, end_at=proposal.end_at,
                        template_id=proposal.template_id),
        actor="employee_demo", customer_ref=customer_ref, now=now,
    )
    print(json.dumps({
        "status": "SIMULATED_ONLY",
        "explicitly_not_committed": True,
        "projection": projection.model_dump(mode="json"),
        "plan": planned.model_dump(mode="json"),
        "preview": preview.model_dump(mode="json"),
        "warning": "No TAL authorization, calendar write, hold, or provider verification.",
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE-AGENT synthetic demos")
    parser.add_argument("command", choices=["demo", "virtual-office-demo"])
    options = parser.parse_args()
    if options.command == "demo":
        _m0_demo()
    else:
        _m1_demo()


if __name__ == "__main__":
    main()
