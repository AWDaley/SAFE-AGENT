"""Offline-only demonstrations; verified effects target synthetic fixtures, never providers."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.contracts import CalendarCreateEvent, Projection, SimulatedChange, Task
from safe_agent.demo import synthetic_fixture_directory
from safe_agent.orchestration import TaskRunner
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar


def _fixtures() -> Path:
    return synthetic_fixture_directory()


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



def _m2_demo() -> None:
    """Run a full synthetic proposal-to-policy-to-ledger cycle, with no writes."""
    from safe_agent.canonical import sha256
    from safe_agent.contracts.control import TaskBudget, TaskManifest
    from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
    from safe_agent.security.principal import Principal
    from safe_agent.storage.repository import LedgerRepository
    from safe_agent.tal.office_gate import OfficeGate

    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    scope = BrokerScope(
        organization_id="org_demo", initiator_id="employee_demo",
        task_id="task_m2_demo", operation="calendar.create_event", now=now,
    )
    broker = ReferenceBroker(secret=b"m2_SYNTHETIC_DEMO_SIGNING_KEY_Never_use_in_production")
    expiry = now + timedelta(days=3)
    calendar_ref = broker.mint(
        resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
        scope=scope, permitted_operations=("calendar.create_event",), expires_at=expiry,
    )
    customer_ref = broker.mint(
        resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("calendar.create_event",), expires_at=expiry,
    )
    office = VirtualOffice(SyntheticOfficeState.from_fixture(
        _fixtures() / "office_state.json"), broker)
    repo = LedgerRepository.testing_sqlite()
    coordinator = TrustedOfficeTasks(repo, office, OfficeGate(broker))
    principal = Principal(
        organization_id="org_demo", actor_id="employee_demo",
        authenticated_until=expiry,
        scopes=("task:create", "task:prepare", "task:propose", "task:read", "task:audit"),
    )
    beginning = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    ending = datetime.fromisoformat("2026-10-15T17:00:00-04:00")
    manifest = TaskManifest(
        manifest_id="manifest_m2_demo", manifest_version=1, task_id="task_m2_demo",
        organization_id="org_demo", initiator_id="employee_demo",
        calendar_ref=calendar_ref, customer_ref=customer_ref,
        permitted_operations=("calendar.create_event",),
        allowed_start=beginning, allowed_end=ending, time_zone="America/New_York",
        template_id="follow_up_v1", budget=TaskBudget(max_cycles=2),
    )
    task = Task(
        task_id="task_m2_demo", organization_id="org_demo", initiator_id="employee_demo",
        manifest_id=manifest.manifest_id, manifest_hash=sha256(manifest),
        objective_code="schedule_follow_up", created_at=now - timedelta(minutes=5),
        deadline=now + timedelta(days=2),
    )
    coordinator.register(principal, task, manifest, now=now)
    projection = office.observe(ObserveCalendar(
        organization_id="org_demo", initiator_id="employee_demo", task_id=task.task_id,
        calendar_ref=calendar_ref, customer_ref=customer_ref,
        from_at=beginning, until_at=ending,
    ), as_of=now)
    # The first *read-only* planning pass determines a candidate. Only the
    # simulator can mint the matching simulation_ref supplied to the second pass.
    context = SafeTaskContext(
        task_id=task.task_id, organization_id="org_demo", calendar_ref=calendar_ref,
        customer_ref=customer_ref, allowed_start=beginning, allowed_end=ending,
        duration_minutes=30, time_zone="America/New_York", template_id="follow_up_v1",
        simulation_ref="simulation_pending",
    )
    runner = TaskRunner(FakePlanner())
    first = runner.plan_once(task, context, projection.to_planner_projection()).proposal
    if not isinstance(first, CalendarCreateEvent):
        raise AssertionError("M2 demo requires a free slot")
    preview = office.simulate(projection, SimulatedChange(
        calendar_ref=first.calendar_ref, customer_ref=first.customer_ref,
        start_at=first.start_at, end_at=first.end_at, template_id=first.template_id,
    ), actor="employee_demo", customer_ref=customer_ref, now=now)
    stored = coordinator.preview(
        principal, task.task_id, projection, preview, expected_state_version=0, now=now)
    planned = runner.plan_once(
        task, context.model_copy(update={"simulation_ref": preview.simulation_ref}),
        projection.to_planner_projection(),
    )
    decision = coordinator.submit_planning_result(
        principal, task.task_id, planned,
        expected_state_version=stored.state_version, now=now)
    replayed = coordinator.replay(principal, task.task_id, now=now)
    print(json.dumps({
        "mode": "M2_SYNTHETIC_POLICY_ONLY_NO_EXTERNAL_EXECUTION",
        "decision": decision.model_dump(mode="json"),
        "state": replayed.model_dump(mode="json"),
        "verified_ledger_event_count": len(coordinator.events(principal, task.task_id, now=now)),
        "external_effects": replayed.external_effects,
        "upstream_tal_calendar_domain_integrated": False,
    }, indent=2))
    repo.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE-AGENT synthetic demos")
    parser.add_argument(
        "command",
        choices=[
            "demo", "virtual-office-demo", "m2-demo",
            "m3-demo", "m4-demo", "m5-demo", "m6-worker-demo",
            "m6-planning-demo", "m6-isolated-model-demo",
        ],
    )
    options = parser.parse_args()
    if options.command == "demo":
        _m0_demo()
    elif options.command == "virtual-office-demo":
        _m1_demo()
    elif options.command == "m2-demo":
        _m2_demo()
    elif options.command == "m3-demo":
        from safe_agent.demo.m3 import print_m3_demo
        print_m3_demo()
    elif options.command == "m4-demo":
        from safe_agent.demo.m4 import print_m4_demo
        print_m4_demo()
    elif options.command == "m5-demo":
        from safe_agent.demo.m5 import print_m5_demo
        print_m5_demo()
    elif options.command == "m6-worker-demo":
        from safe_agent.demo.m6_worker import print_m6_worker_demo
        print_m6_worker_demo()
    elif options.command == "m6-planning-demo":
        from safe_agent.demo.m6_planning import print_m6_planning_demo

        print_m6_planning_demo()
    else:
        from safe_agent.demo.m6_isolated import print_m6_isolated_model_demo

        print_m6_isolated_model_demo()


if __name__ == "__main__":
    main()
