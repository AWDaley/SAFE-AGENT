"""Calendar simulation conformance: never authoritative; no source mutations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from safe_agent.canonical import sha256
from safe_agent.contracts.office import SimulatedChange
from safe_agent.virtual_office import StaleProjection, SyntheticOfficeState, UnissuedProjection, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar


def _change(calendar_request: ObserveCalendar, start: str, end: str,
            template: str = "follow_up_v1") -> SimulatedChange:
    return SimulatedChange(
        calendar_ref=calendar_request.calendar_ref,
        customer_ref=calendar_request.customer_ref,
        start_at=datetime.fromisoformat(start),
        end_at=datetime.fromisoformat(end),
        template_id=template,
    )


def test_availability_subtracts_bookings(office: VirtualOffice, calendar_request: ObserveCalendar,
                                         now: datetime) -> None:
    projection = office.observe(calendar_request, as_of=now)
    assert [(s.start_at.hour, s.start_at.minute, s.end_at.hour, s.end_at.minute)
            for s in projection.free_slots] == [(13, 0, 14, 0), (14, 30, 17, 0)]
    assert projection.source_revision == "main_rev_1"
    assert projection.classification == "availability_only"
    assert projection.to_planner_projection().free_slots == projection.free_slots


def test_deterministic_projection_and_simulation(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime
) -> None:
    p1 = office.observe(calendar_request, as_of=now)
    p2 = office.observe(calendar_request, as_of=now)
    assert p1 == p2
    change = _change(calendar_request, "2026-10-15T13:00:00-04:00", "2026-10-15T13:30:00-04:00")
    s1 = office.simulate(p1, change, actor="employee_demo", customer_ref=calendar_request.customer_ref,
                         now=now)
    s2 = office.simulate(p2, change, actor="employee_demo", customer_ref=calendar_request.customer_ref,
                         now=now)
    assert s1 == s2
    assert s1.advisory_only and not s1.conflicts
    assert s1.expected_postconditions == ("event_created", "matching_time")
    assert s1.simulation_hash == s2.simulation_hash
    assert s1.change_diff.before is None
    assert "NOT_A_RESERVATION" in s1.warnings


def test_overlapping_booking_is_a_conflict_without_changes(
    source: SyntheticOfficeState, office: VirtualOffice, calendar_request: ObserveCalendar,
    now: datetime
) -> None:
    before = sha256(source)
    projection = office.observe(calendar_request, as_of=now)
    preview = office.simulate(
        projection,
        _change(calendar_request, "2026-10-15T14:00:00-04:00", "2026-10-15T14:30:00-04:00"),
        actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now,
    )
    assert "BOOKING_OVERLAP" in preview.conflicts
    assert preview.expected_postconditions == ()
    assert sha256(source) == before
    assert len(source.calendar("cal_main_private").bookings) == 3


def test_half_open_adjacent_events_do_not_overlap(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime
) -> None:
    projection = office.observe(calendar_request, as_of=now)
    preview = office.simulate(
        projection,
        _change(calendar_request, "2026-10-15T14:30:00-04:00", "2026-10-15T15:00:00-04:00"),
        actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now,
    )
    assert not preview.conflicts


@pytest.mark.parametrize("begin,end,reason", [
    ("2026-10-15T12:30:00-04:00", "2026-10-15T13:00:00-04:00", "OUTSIDE_REQUEST_WINDOW"),
    ("2026-10-15T16:45:00-04:00", "2026-10-15T17:15:00-04:00", "OUTSIDE_OFFICE_HOURS"),
    ("2026-10-15T13:00:00-04:00", "2026-10-15T13:45:00-04:00", "TEMPLATE_DURATION_MISMATCH"),
    ("2026-10-15T13:00:00-04:00", "2026-10-15T13:30:00-05:00", "TIME_ZONE_MISMATCH"),
])
def test_restricted_times_have_explicit_conflict(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime,
    begin: str, end: str, reason: str
) -> None:
    projection = office.observe(calendar_request, as_of=now)
    result = office.simulate(projection, _change(calendar_request, begin, end), actor="employee_demo",
                             customer_ref=calendar_request.customer_ref, now=now)
    assert reason in result.conflicts
    assert not result.expected_postconditions


def test_unknown_template_is_denied_by_simulation(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime
) -> None:
    p = office.observe(calendar_request, as_of=now)
    result = office.simulate(
        p, _change(calendar_request, "2026-10-15T13:00:00-04:00",
                   "2026-10-15T13:30:00-04:00", "invented_template"),
        actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now,
    )
    assert result.conflicts == ("UNKNOWN_TEMPLATE",)


def test_expired_projection_denied(office: VirtualOffice, calendar_request: ObserveCalendar,
                                  now: datetime) -> None:
    projection = office.observe(calendar_request, as_of=now)
    with pytest.raises(StaleProjection, match="expired"):
        office.simulate(projection,
                        _change(calendar_request, "2026-10-15T13:00:00-04:00",
                                "2026-10-15T13:30:00-04:00"),
                        actor="employee_demo", customer_ref=calendar_request.customer_ref,
                        now=now + timedelta(minutes=10))


def test_changed_source_revision_denied(
    office: VirtualOffice, source: SyntheticOfficeState, calendar_request: ObserveCalendar,
    now: datetime
) -> None:
    projection = office.observe(calendar_request, as_of=now)
    calendars = list(source.calendars)
    calendars[0] = calendars[0].model_copy(update={"source_revision": "main_rev_2"})
    office.replace_synthetic_source(source.model_copy(update={"calendars": tuple(calendars)}))
    with pytest.raises(StaleProjection, match="source revision"):
        office.simulate(projection,
                        _change(calendar_request, "2026-10-15T13:00:00-04:00",
                                "2026-10-15T13:30:00-04:00"),
                        actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now)


def test_changed_projection_fields_rejected(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime
) -> None:
    p = office.observe(calendar_request, as_of=now)
    forged = p.model_copy(update={"free_slots": ()})
    with pytest.raises(UnissuedProjection):
        office.simulate(forged,
                        _change(calendar_request, "2026-10-15T13:00:00-04:00",
                                "2026-10-15T13:30:00-04:00"),
                        actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now)


def test_no_private_customer_fields_in_projection_or_preview(
    office: VirtualOffice, source: SyntheticOfficeState, calendar_request: ObserveCalendar,
    now: datetime
) -> None:
    p = office.observe(calendar_request, as_of=now)
    change = _change(calendar_request, "2026-10-15T13:00:00-04:00", "2026-10-15T13:30:00-04:00")
    s = office.simulate(p, change, actor="employee_demo", customer_ref=calendar_request.customer_ref,
                        now=now)
    serialized = p.model_dump_json() + s.model_dump_json()
    for customer in source.customers:
        assert customer.name not in serialized
        assert customer.customer_id not in serialized
        if customer.email:
            assert customer.email not in serialized
    for cal in source.calendars:
        assert cal.calendar_id not in serialized
        for booking in cal.bookings:
            assert booking.booking_id not in serialized
    assert "ref_" in serialized and "binding_" not in serialized


def test_unissued_projection_cannot_be_simulated_by_second_office(
    source: SyntheticOfficeState, broker, office: VirtualOffice,
    calendar_request: ObserveCalendar, now: datetime
) -> None:
    issued = office.observe(calendar_request, as_of=now)
    second = VirtualOffice(source, broker)
    with pytest.raises(UnissuedProjection):
        second.simulate(
            issued, _change(calendar_request, "2026-10-15T13:00:00-04:00",
                            "2026-10-15T13:30:00-04:00"),
            actor="employee_demo", customer_ref=calendar_request.customer_ref, now=now,
        )


def test_cross_tenant_observation_denied(
    office: VirtualOffice, calendar_request: ObserveCalendar, now: datetime
) -> None:
    foreign = calendar_request.model_copy(update={"organization_id": "org_foreign"})
    with pytest.raises(ValueError, match="organization"):
        office.observe(foreign, as_of=now)


def test_no_unrecognized_fields_or_naive_datetimes(calendar_request: ObserveCalendar) -> None:
    with pytest.raises(ValidationError):
        ObserveCalendar.model_validate({**calendar_request.model_dump(), "private_email": "secret"})
    with pytest.raises(ValidationError):
        ObserveCalendar.model_validate({**calendar_request.model_dump(), "from_at": datetime(2026, 10, 15)})


def test_synthetic_state_fixture_is_not_model_view(source: SyntheticOfficeState) -> None:
    assert len(source.calendars) == 2 and len(source.customers) >= 5
    assert any(c.email is None for c in source.customers)
    assert len(source.appointment_templates) >= 2
    assert len(source.internal_tasks) >= 2
    assert "example.invalid" in json.dumps(source.model_dump(mode="json"))
