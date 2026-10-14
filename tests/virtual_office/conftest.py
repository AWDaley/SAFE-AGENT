"""All M1 tests use synthetic records and an explicit deterministic clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def now() -> datetime:
    return datetime.fromisoformat("2026-10-14T13:00:00-04:00")


@pytest.fixture
def source() -> SyntheticOfficeState:
    return SyntheticOfficeState.from_fixture(ROOT / "fixtures/synthetic_office/office_state.json")


@pytest.fixture
def broker() -> ReferenceBroker:
    return ReferenceBroker(secret=b"test_only_broker_signing_key_32bytes_minimum_42")


@pytest.fixture
def handles(broker: ReferenceBroker, now: datetime) -> tuple[str, str]:
    scope = BrokerScope(organization_id="org_demo", initiator_id="employee_demo",
                        task_id="task_m1", operation="calendar.create_event", now=now)
    expires = now + timedelta(days=10)
    calendar = broker.mint(
        resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
        scope=scope, permitted_operations=("calendar.create_event",), expires_at=expires,
    )
    customer = broker.mint(
        resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("calendar.create_event", "tasks.create"),
        expires_at=expires,
    )
    return calendar, customer


@pytest.fixture
def office(source: SyntheticOfficeState, broker: ReferenceBroker) -> VirtualOffice:
    return VirtualOffice(source, broker)


@pytest.fixture
def calendar_request(handles: tuple[str, str]) -> ObserveCalendar:
    cal, customer = handles
    return ObserveCalendar(
        organization_id="org_demo", initiator_id="employee_demo", task_id="task_m1",
        calendar_ref=cal, customer_ref=customer,
        from_at=datetime.fromisoformat("2026-10-15T13:00:00-04:00"),
        until_at=datetime.fromisoformat("2026-10-15T17:00:00-04:00"),
    )
