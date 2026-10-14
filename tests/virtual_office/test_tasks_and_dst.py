"""Internal task simulation and DST safety (M1 remains advisory only)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.office import SimulatedChange
from safe_agent.virtual_office import StaleProjection, SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import InternalTaskChange, ObserveCalendar, ObserveTasks
from safe_agent.virtual_office.timezones import (
    CalendarTimeError,
    checked_instant,
    checked_local_time,
)


def test_internal_task_creation_is_preview_only(
    source: SyntheticOfficeState, office: VirtualOffice, now: datetime,
    handles: tuple[str, str]
) -> None:
    customer = handles[1]
    request = ObserveTasks(organization_id="org_demo", initiator_id="employee_demo",
                           task_id="task_m1", customer_ref=customer)
    before = sha256(source)
    projection = office.observe_tasks(request, as_of=now)
    assert len(projection.tasks) == 1
    assert projection.tasks[0].task_code == "call_back"
    change = InternalTaskChange(customer_ref=customer, task_code="prepare_documents",
                                due_at=datetime.fromisoformat("2026-10-17T10:00:00-04:00"))
    result = office.simulate_task(projection, change, actor="employee_demo", now=now)
    assert not result.conflicts and result.expected_postconditions == ("task_created",)
    assert result.advisory_only and result.warnings == ("NOT_A_COMMIT",)
    assert sha256(source) == before
    assert len(source.internal_tasks) == 2
    assert result == office.simulate_task(projection, change, actor="employee_demo", now=now)
    assert "Ada Synthetic" not in projection.model_dump_json() + result.model_dump_json()


def test_internal_task_conflicts_and_stale_projection(
    source: SyntheticOfficeState, office: VirtualOffice, now: datetime,
    handles: tuple[str, str]
) -> None:
    customer = handles[1]
    request = ObserveTasks(organization_id="org_demo", initiator_id="employee_demo",
                           task_id="task_m1", customer_ref=customer)
    projection = office.observe_tasks(request, as_of=now)
    existing = InternalTaskChange(customer_ref=customer, task_code="call_back",
                                  due_at=datetime.fromisoformat("2026-10-16T15:00:00-04:00"))
    duplicate = office.simulate_task(projection, existing, actor="employee_demo", now=now)
    assert duplicate.conflicts == ("DUPLICATE_TASK",)
    past = InternalTaskChange(customer_ref=customer, task_code="call_back",
                              due_at=now - timedelta(days=1))
    result = office.simulate_task(projection, past, actor="employee_demo", now=now)
    assert "DUE_DATE_NOT_FUTURE" in result.conflicts
    office.replace_synthetic_source(
        source.model_copy(update={"task_list_revision": "tasks_source_rev_2"})
    )
    with pytest.raises(StaleProjection):
        office.simulate_task(projection, existing, actor="employee_demo", now=now)


def test_task_scope_mismatch_denied(office: VirtualOffice, now: datetime,
                                    handles: tuple[str, str]) -> None:
    req = ObserveTasks(organization_id="org_demo", initiator_id="employee_demo",
                       task_id="task_m1", customer_ref=handles[1])
    proj = office.observe_tasks(req, as_of=now)
    change = InternalTaskChange(customer_ref="ref_forged", task_code="call_back",
                                due_at=now + timedelta(days=1))
    with pytest.raises(ValueError, match="scope mismatch"):
        office.simulate_task(proj, change, actor="employee_demo", now=now)


def test_dst_gap_and_fold_rejected() -> None:
    zone = ZoneInfo("America/New_York")
    with pytest.raises(CalendarTimeError, match="ambiguous"):
        checked_instant(datetime.fromisoformat("2026-11-01T01:30:00-04:00"), zone)
    with pytest.raises(CalendarTimeError, match="ambiguous"):
        checked_instant(datetime.fromisoformat("2026-11-01T01:30:00-05:00"), zone)
    with pytest.raises(CalendarTimeError, match="nonexistent"):
        checked_instant(datetime.fromisoformat("2027-03-14T02:30:00-05:00"), zone)
    assert checked_instant(datetime.fromisoformat("2027-03-14T03:30:00-04:00"), zone).hour == 3
    with pytest.raises(CalendarTimeError, match="ambiguous"):
        checked_local_time(datetime(2026, 11, 1).date(), datetime(2026, 11, 1, 1, 30).time(), zone)


@pytest.mark.parametrize("when,from_at,until_at,expected", [
    ("2026-10-31T13:00:00-04:00", "2026-11-01T00:30:00-04:00",
     "2026-11-01T03:30:00-05:00", "DST_AMBIGUOUS"),
    ("2027-03-13T13:00:00-05:00", "2027-03-14T00:30:00-05:00",
     "2027-03-14T03:30:00-04:00", "DST_NONEXISTENT"),
])
def test_dst_conflict_in_full_calendar_simulation(
    source: SyntheticOfficeState, when: str, from_at: str,
    until_at: str, expected: str
) -> None:
    now = datetime.fromisoformat(when)
    broker = ReferenceBroker(secret=b"test_only_broker_signing_key_32bytes_minimum_42")
    scope = BrokerScope(organization_id="org_demo", initiator_id="employee_demo",
                        task_id="task_dst", operation="calendar.create_event", now=now)
    cal = broker.mint(resource_id="cal_front_desk_private", resource_kind=ResourceKind.CALENDAR,
                      scope=scope, permitted_operations=("calendar.create_event",),
                      expires_at=now + timedelta(days=2))
    cust = broker.mint(resource_id="cust_c5", resource_kind=ResourceKind.CUSTOMER,
                       scope=scope, permitted_operations=("calendar.create_event",),
                       expires_at=now + timedelta(days=2))
    office = VirtualOffice(source, broker)
    req = ObserveCalendar(organization_id="org_demo", initiator_id="employee_demo",
                          task_id="task_dst", calendar_ref=cal, customer_ref=cust,
                          from_at=datetime.fromisoformat(from_at),
                          until_at=datetime.fromisoformat(until_at))
    p = office.observe(req, as_of=now)
    if expected == "DST_AMBIGUOUS":
        start = "2026-11-01T01:30:00-04:00"
        end = "2026-11-01T02:00:00-05:00"
    else:
        start = "2027-03-14T02:30:00-05:00"
        end = "2027-03-14T03:00:00-04:00"
    # The simulator reports DST and cannot mark this preview valid.
    # For invalid spring local instants, the ISO times can overlap in UTC;
    # SimulatedChange still requires an increasing UTC interval.
    if expected == "DST_NONEXISTENT":
        end = "2027-03-14T04:00:00-04:00"
    change = SimulatedChange(calendar_ref=cal, customer_ref=cust,
                             start_at=datetime.fromisoformat(start),
                             end_at=datetime.fromisoformat(end), template_id="follow_up_v1")
    preview = office.simulate(p, change, actor="employee_demo", customer_ref=cust, now=now)
    assert expected in preview.conflicts
    assert preview.expected_postconditions == ()
    assert preview.advisory_only


def test_dst_fixture_exists() -> None:
    import json
    path = Path(__file__).resolve().parents[2] / "fixtures/synthetic_office/dst_cases.json"
    samples = json.loads(path.read_text())
    assert samples["time_zone"] == "America/New_York"
    assert "01:30" in samples["fall_ambiguous"]
