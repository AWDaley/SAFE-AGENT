"""Runnable self-contained synthetic-only office scheduling proof (no secrets)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.agent.fake import FakePlanner
from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Task, TaskBudget, TaskManifest
from safe_agent.contracts.office import SimulatedChange
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.orchestration.runner import TaskRunner
from safe_agent.security.principal import Principal
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar


def run_m3_demo() -> dict[str, object]:
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    start = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    end = datetime.fromisoformat("2026-10-15T17:00:00-04:00")
    fixture = (Path(__file__).resolve().parents[3]
               / "fixtures/synthetic_office/office_state.json")
    source = SyntheticOfficeState.from_fixture(fixture)
    # A deterministic key is safe ONLY because these are bundled fake test records.
    broker = ReferenceBroker(secret=b"m3_synthetic_test_only_key_not_a_provider_secret_111")
    scope = BrokerScope(
        organization_id="org_demo", initiator_id="employee_demo", task_id="task_m3_demo",
        operation="calendar.create_event", now=now,
    )
    expiry = now + timedelta(days=3)
    cal = broker.mint(
        resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
        scope=scope, permitted_operations=("calendar.create_event",), expires_at=expiry,
    )
    customer = broker.mint(
        resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("calendar.create_event",), expires_at=expiry,
    )
    principal = Principal(
        organization_id="org_demo", actor_id="employee_demo",
        authenticated_until=expiry,
        scopes=("task:create", "task:prepare", "task:propose", "task:read",
                "task:audit", "task:execute"),
    )
    manifest = TaskManifest(
        manifest_id="manifest_m3_demo", manifest_version=1, task_id="task_m3_demo",
        organization_id="org_demo", initiator_id="employee_demo",
        calendar_ref=cal, customer_ref=customer,
        permitted_operations=("calendar.create_event",),
        allowed_start=start, allowed_end=end, template_id="follow_up_v1",
        time_zone="America/New_York", budget=TaskBudget(max_cycles=3),
    )
    task = Task(
        task_id="task_m3_demo", organization_id="org_demo", initiator_id="employee_demo",
        manifest_id=manifest.manifest_id, manifest_hash=sha256(manifest),
        objective_code="schedule_follow_up", created_at=now - timedelta(minutes=5),
        deadline=now + timedelta(days=2),
    )
    adapter = FakeCalendarAdapter(source)
    office = VirtualOffice(source, broker)
    repo = LedgerRepository.testing_sqlite()
    gate = OfficeGate(broker)
    coordinator = TrustedOfficeTasks(
        repo, office, gate,
        synthetic_gateway=SyntheticExecutionGateway(repo, office, gate, broker, adapter),
    )
    try:
        coordinator.register(principal, task, manifest, now=now)
        observed = office.observe(ObserveCalendar(
            organization_id=task.organization_id, initiator_id=task.initiator_id,
            task_id=task.task_id, calendar_ref=cal, customer_ref=customer,
            from_at=start, until_at=end,
        ), as_of=now)
        context = SafeTaskContext(
            task_id=task.task_id, organization_id=task.organization_id,
            calendar_ref=cal, customer_ref=customer,
            allowed_start=start, allowed_end=end,
            duration_minutes=30, time_zone=manifest.time_zone,
            template_id=manifest.template_id, simulation_ref="simulation_pending",
        )
        runner = TaskRunner(FakePlanner())
        candidate = runner.plan_once(task, context, observed.to_planner_projection()).proposal
        if not isinstance(candidate, CalendarCreateEvent):
            raise ValueError("synthetic demo has no available appointment")
        preview = office.simulate(observed, SimulatedChange(
            calendar_ref=cal, customer_ref=customer,
            start_at=candidate.start_at, end_at=candidate.end_at,
            template_id=candidate.template_id,
        ), actor=task.initiator_id, customer_ref=customer, now=now)
        saved = coordinator.preview(
            principal, task.task_id, observed, preview,
            expected_state_version=0, now=now,
        )
        planned = runner.plan_once(
            task, context.model_copy(update={"simulation_ref": preview.simulation_ref}),
            observed.to_planner_projection(),
        )
        admission = coordinator.submit_planning_result(
            principal, task.task_id, planned,
            expected_state_version=saved.state_version, now=now,
        )
        before = adapter.reader().snapshot(
            calendar_id="cal_main_private", organization_id="org_demo",
        )
        eligible = coordinator.inspect(principal, task.task_id, now=now)
        receipt = coordinator.execute_synthetic(
            principal, task.task_id, now=now, expected_state_version=eligible.state_version,
        )
        after = adapter.reader().snapshot(
            calendar_id="cal_main_private", organization_id="org_demo",
        )
        verified = coordinator.replay(principal, task.task_id, now=now)
        events = coordinator.events(principal, task.task_id, now=now)
        return {
            "environment": "SYNTHETIC_FIXTURE_ONLY",
            "upstream_tal_calendar_domain_certified": False,
            "provider_network_access": False,
            "admission": admission.model_dump(mode="json"),
            "preview": {"simulation_ref": preview.simulation_ref,
                        "conflicts": preview.conflicts,
                        "advisory_only": preview.advisory_only},
            "before": {"booking_count": len(before.calendar.bookings),
                       "source_hash": before.source_hash},
            "after": {"booking_count": len(after.calendar.bookings),
                      "source_hash": after.source_hash},
            "receipt": receipt.model_dump(mode="json"),
            "snapshot": verified.model_dump(mode="json"),
            "effect_count": adapter.effect_count,
            "timeline": [
                {"sequence": event.ledger_sequence, "kind": event.kind.value,
                 "event_hash": event.event_hash}
                for event in events
            ],
        }
    finally:
        repo.close()


def print_m3_demo() -> None:
    print(json.dumps(run_m3_demo(), indent=2))
