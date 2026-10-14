"""Scoped handles are non-bearer pointers into a separate trusted reference map."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from safe_agent.broker import BrokerScope, ReferenceBroker, ReferenceDenied, ResourceKind
from safe_agent.virtual_office.models import ObserveCalendar


def _scope(now: datetime, *, organization: str = "org_demo", actor: str = "employee_demo",
           task: str = "task_m1", operation: str = "calendar.create_event") -> BrokerScope:
    return BrokerScope.model_validate({
        "organization_id": organization, "initiator_id": actor,
        "task_id": task, "operation": operation, "now": now,
    })


def test_handle_is_opaque_and_scope_bound(broker: ReferenceBroker, now: datetime) -> None:
    scope = _scope(now)
    handle = broker.mint(
        resource_id="cust_secret_private", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("calendar.create_event",),
        expires_at=now + timedelta(hours=1),
    )
    assert handle.startswith("ref_")
    assert "secret" not in handle
    assert broker.resolve(handle, scope=scope, kind=ResourceKind.CUSTOMER) == "cust_secret_private"
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=_scope(now, organization="org_other"), kind=ResourceKind.CUSTOMER)
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=_scope(now, task="task_other"), kind=ResourceKind.CUSTOMER)
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=_scope(now, actor="employee_other"), kind=ResourceKind.CUSTOMER)
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=_scope(now, operation="tasks.create"), kind=ResourceKind.CUSTOMER)
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=scope, kind=ResourceKind.CALENDAR)


def test_expired_revoked_and_unknown_handles(broker: ReferenceBroker, now: datetime) -> None:
    scope = _scope(now)
    handle = broker.mint(resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
                         scope=scope, permitted_operations=("calendar.create_event",),
                         expires_at=now + timedelta(minutes=5))
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=_scope(now + timedelta(minutes=5)),
                       kind=ResourceKind.CUSTOMER)
    broker.revoke(handle)
    with pytest.raises(ReferenceDenied):
        broker.resolve(handle, scope=scope, kind=ResourceKind.CUSTOMER)
    with pytest.raises(ReferenceDenied):
        broker.resolve("ref_unknown", scope=scope, kind=ResourceKind.CUSTOMER)


def test_mint_does_not_accept_expired_or_unauthorized_scope(
    broker: ReferenceBroker, now: datetime
) -> None:
    with pytest.raises(ReferenceDenied):
        broker.mint(resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
                    scope=_scope(now), permitted_operations=("tasks.create",),
                    expires_at=now + timedelta(hours=1))
    with pytest.raises(ReferenceDenied):
        broker.mint(resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
                    scope=_scope(now), permitted_operations=("calendar.create_event",),
                    expires_at=now)


def test_broker_is_required_even_with_correct_handle(
    office, calendar_request: ObserveCalendar, now: datetime, broker: ReferenceBroker
) -> None:
    # A handle learned from an earlier projection is not a bearer authorization.
    office.observe(calendar_request, as_of=now)
    broker.revoke(calendar_request.customer_ref)
    with pytest.raises(ReferenceDenied):
        office.observe(calendar_request, as_of=now)


def test_other_actor_cannot_simulate_issued_projection(office, calendar_request, now) -> None:
    from safe_agent.contracts.office import SimulatedChange

    p = office.observe(calendar_request, as_of=now)
    change = SimulatedChange(
        calendar_ref=calendar_request.calendar_ref, customer_ref=calendar_request.customer_ref,
        start_at=datetime.fromisoformat("2026-10-15T13:00:00-04:00"),
        end_at=datetime.fromisoformat("2026-10-15T13:30:00-04:00"),
        template_id="follow_up_v1",
    )
    with pytest.raises(ReferenceDenied):
        office.simulate(p, change, actor="other_employee", customer_ref=calendar_request.customer_ref,
                        now=now)
