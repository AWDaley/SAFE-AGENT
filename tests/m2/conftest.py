"""Synthetic M2 state, explicit clock, isolated SQLite; no live providers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Task, TaskBudget, TaskManifest
from safe_agent.contracts.office import SimulatedChange
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.security.principal import Principal
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
SLOT_START = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
SLOT_END = SLOT_START + timedelta(minutes=30)
WINDOW_END = datetime.fromisoformat("2026-10-15T17:00:00-04:00")


@pytest.fixture
def kit(tmp_path):
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'm2.sqlite'}")
    broker = ReferenceBroker(secret=b"m2_test_private_broker_key_never_prod_000001")
    scope = BrokerScope(organization_id="org_demo", initiator_id="employee_demo",
                        task_id="task_m2", operation="calendar.create_event", now=NOW)
    expiry = NOW + timedelta(days=3)
    cal = broker.mint(resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
                      scope=scope, permitted_operations=("calendar.create_event",),
                      expires_at=expiry)
    cust = broker.mint(resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
                       scope=scope, permitted_operations=("calendar.create_event",),
                       expires_at=expiry)
    office = VirtualOffice(SyntheticOfficeState.from_fixture(
        ROOT / "fixtures/synthetic_office/office_state.json"), broker)
    coordinator = TrustedOfficeTasks(repo, office, OfficeGate(broker))
    principal = Principal(
        organization_id="org_demo", actor_id="employee_demo",
        scopes=("task:create", "task:prepare", "task:propose", "task:read", "task:audit"),
        authenticated_until=NOW + timedelta(days=2),
    )
    manager = Principal(organization_id="org_demo", actor_id="manager_demo",
                        scopes=("task:approve", "task:read", "task:audit"),
                        authenticated_until=NOW + timedelta(days=2))
    manifest = TaskManifest(
        manifest_id="manifest_m2", manifest_version=1,
        organization_id="org_demo", initiator_id="employee_demo", task_id="task_m2",
        calendar_ref=cal, customer_ref=cust,
        permitted_operations=("calendar.create_event",),
        allowed_start=SLOT_START, allowed_end=WINDOW_END,
        max_duration_minutes=60, time_zone="America/New_York",
        template_id="follow_up_v1", budget=TaskBudget(max_cycles=2),
    )
    task = Task(
        task_id="task_m2", organization_id="org_demo", initiator_id="employee_demo",
        manifest_id="manifest_m2", manifest_hash=sha256(manifest),
        objective_code="schedule_follow_up",
        created_at=NOW - timedelta(minutes=10), deadline=NOW + timedelta(days=2),
    )
    try:
        yield {"repo": repo, "broker": broker, "office": office,
               "coordinator": coordinator, "principal": principal, "manager": manager,
               "manifest": manifest, "task": task, "calendar_ref": cal, "customer_ref": cust}
    finally:
        repo.close()


def arrange(kit, *, approval: bool = False):
    coordinator = kit["coordinator"]
    manifest = kit["manifest"].model_copy(update={"requires_approval": approval})
    task = kit["task"].model_copy(update={"manifest_hash": sha256(manifest)})
    coordinator.register(kit["principal"], task, manifest, now=NOW)
    observed = kit["office"].observe(
        ObserveCalendar(
            organization_id="org_demo", initiator_id="employee_demo", task_id="task_m2",
            calendar_ref=kit["calendar_ref"], customer_ref=kit["customer_ref"],
            from_at=SLOT_START, until_at=WINDOW_END,
        ), as_of=NOW,
    )
    change = SimulatedChange(
        calendar_ref=kit["calendar_ref"], customer_ref=kit["customer_ref"],
        start_at=SLOT_START, end_at=SLOT_END, template_id="follow_up_v1",
    )
    preview = kit["office"].simulate(observed, change,
                                      actor="employee_demo", customer_ref=kit["customer_ref"],
                                      now=NOW)
    state = coordinator.preview(kit["principal"], "task_m2", observed, preview,
                                expected_state_version=0, now=NOW)
    proposal = CalendarCreateEvent(
        calendar_ref=change.calendar_ref, customer_ref=change.customer_ref,
        start_at=change.start_at, end_at=change.end_at, time_zone="America/New_York",
        template_id=change.template_id, simulation_ref=preview.simulation_ref,
        projection_rev=observed.revision,
    )
    return coordinator, state, observed, preview, proposal
