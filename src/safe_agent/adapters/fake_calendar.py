"""Trusted, *synthetic-only* calendar state and independent read/write surfaces.

The fake adapter is not a production provider client, and has no network access.
Only the trusted execution gateway receives its write interface. Model-facing
planning receives sanitized VirtualOffice projections, never this object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from safe_agent.canonical import sha256
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.virtual_office.service import _interval_overlaps, _working_intervals
from safe_agent.virtual_office.state import (
    PrivateBooking,
    PrivateCalendar,
    SyntheticOfficeState,
)


class FakeCalendarConflict(ValueError):
    """The synthetic calendar no longer matches the expected source state."""


@dataclass(frozen=True)
class TrustedCalendarRead:
    """Contains private resource identifiers; trusted components only."""

    calendar: PrivateCalendar
    source_hash: str


class FakeCalendarReader:
    """Separate trusted read-back surface; intentionally has no create method."""

    def __init__(self, adapter: FakeCalendarAdapter) -> None:
        self.__adapter = adapter

    def snapshot(self, *, calendar_id: str, organization_id: str) -> TrustedCalendarRead:
        return self.__adapter._read_calendar(calendar_id, organization_id)

    def booking(self, *, calendar_id: str, organization_id: str,
                event_ref: str) -> PrivateBooking | None:
        return self.__adapter._read_booking(calendar_id, organization_id, event_ref)


class FakeCalendarWriter:
    """Synthetic conditional creation. No public connector, secrets or network."""

    def __init__(self, adapter: FakeCalendarAdapter) -> None:
        self.__adapter = adapter

    def create(
        self, *, calendar_id: str, organization_id: str,
        customer_id: str, start_at: datetime, end_at: datetime,
        template_id: str, expected_revision: str, expected_source_hash: str,
        action_hash: str,
    ) -> str:
        return self.__adapter._commit(
            calendar_id=calendar_id, organization_id=organization_id,
            customer_id=customer_id, start_at=start_at, end_at=end_at,
            template_id=template_id, expected_revision=expected_revision,
            expected_source_hash=expected_source_hash, action_hash=action_hash,
        )


class FakeCalendarAdapter:
    """Versioned synthetic source of truth; idempotent IDs are *not* M4 recovery.

    A second invocation using the same action hash is deliberately rejected,
    rather than silently replayed. Durable crash-safe reconciliation is M4.
    """

    def __init__(self, source: SyntheticOfficeState, *,
                 durable_store: DurableFakeProvider | None = None) -> None:
        if source.schema_version != "safe_agent.synthetic_office.v1":
            raise ValueError("synthetic fixture only")
        self.__state = source
        self._durable = durable_store
        self.__lock = RLock()
        self.__effects: dict[str, str] = {}
        self.__effect_count = 0
        self.__readback_error = False

    @property
    def supports_idempotency(self) -> bool:
        return self._durable is not None

    def find_effect(self, *, organization_id: str, action_hash: str,
                    request_hash: str | None = None) -> str | None:
        if self._durable is not None:
            return self._durable.find_effect(
                organization_id=organization_id, action_hash=action_hash,
                operation="calendar.create_event", request_hash=request_hash,
            )
        if organization_id != self.__state.organization_id:
            raise FakeCalendarConflict("organization mismatch")
        return self.__effects.get(action_hash)

    @property
    def effect_count(self) -> int:
        with self.__lock:
            if self._durable is not None:
                from sqlalchemy import func, select
                from safe_agent.storage.models import FakeProviderEffectRow
                with self._durable._sessions() as session:
                    return int(session.scalar(select(func.count()).select_from(
                        FakeProviderEffectRow).where(
                            FakeProviderEffectRow.organization_id == self.__state.organization_id,
                            FakeProviderEffectRow.operation == "calendar.create_event",
                        )) or 0)
            return self.__effect_count

    def reader(self) -> FakeCalendarReader:
        return FakeCalendarReader(self)

    def trusted_writer(self) -> FakeCalendarWriter:
        return FakeCalendarWriter(self)

    def synthetic_state(self) -> SyntheticOfficeState:
        """Trusted test/sync hook. Never return this object to a model."""
        with self.__lock:
            if self._durable is not None:
                return self._durable.snapshot(self.__state.organization_id)
            return self.__state

    def inject_readback_failure(self, active: bool) -> None:
        """Trusted test-only fault injection."""
        self.__readback_error = active

    def replace_source_for_test(self, source: SyntheticOfficeState) -> None:
        """Trusted concurrency fixture. Never a model-facing operation."""
        with self.__lock:
            if self._durable is not None:
                raise FakeCalendarConflict("durable provider cannot be overwritten by test hook")
            if source.organization_id != self.__state.organization_id:
                raise FakeCalendarConflict("synthetic organization mismatch")
            self.__state = source

    def _read_calendar(self, calendar_id: str, organization_id: str) -> TrustedCalendarRead:
        with self.__lock:
            state = self.synthetic_state()
            if organization_id != state.organization_id:
                raise FakeCalendarConflict("calendar outside permitted organization")
            try:
                calendar = state.calendar(calendar_id)
            except StopIteration as error:
                raise FakeCalendarConflict("unknown calendar") from error
            if calendar.organization_id != organization_id:
                raise FakeCalendarConflict("calendar outside permitted organization")
            return TrustedCalendarRead(calendar=calendar, source_hash=sha256(calendar))

    def _read_booking(self, calendar_id: str, organization_id: str,
                      event_ref: str) -> PrivateBooking | None:
        if self.__readback_error:
            raise ConnectionError("synthetic independent read-back unavailable")
        snapshot = self._read_calendar(calendar_id, organization_id)
        return next((b for b in snapshot.calendar.bookings if b.booking_id == event_ref), None)

    def _commit(
        self, *, calendar_id: str, organization_id: str, customer_id: str,
        start_at: datetime, end_at: datetime, template_id: str,
        expected_revision: str, expected_source_hash: str, action_hash: str,
    ) -> str:
        if self._durable is not None:
            request_hash = sha256({
                "calendar_id": calendar_id, "organization_id": organization_id,
                "customer_id": customer_id, "start_at": start_at, "end_at": end_at,
                "template_id": template_id, "expected_revision": expected_revision,
                "expected_source_hash": expected_source_hash,
            })

            def mutate(source: SyntheticOfficeState) -> tuple[SyntheticOfficeState, str]:
                ephemeral = FakeCalendarAdapter(source)
                ref = ephemeral._commit(
                    calendar_id=calendar_id, organization_id=organization_id,
                    customer_id=customer_id, start_at=start_at, end_at=end_at,
                    template_id=template_id, expected_revision=expected_revision,
                    expected_source_hash=expected_source_hash, action_hash=action_hash,
                )
                return ephemeral.synthetic_state(), ref

            return self._durable.apply(
                organization_id=organization_id, action_hash=action_hash,
                operation="calendar.create_event", request_hash=request_hash,
                mutation=mutate,
            ).effect_ref
        with self.__lock:
            current = self._read_calendar(calendar_id, organization_id)
            calendar = current.calendar
            if (calendar.source_revision != expected_revision
                    or current.source_hash != expected_source_hash):
                raise FakeCalendarConflict("stale calendar revision/fingerprint")
            if action_hash in self.__effects:
                raise FakeCalendarConflict("duplicate action; reconcile before retry")
            try:
                customer = self.__state.customer(customer_id)
                template = self.__state.template(template_id)
            except StopIteration as error:
                raise FakeCalendarConflict("missing synthetic customer/template") from error
            if customer.organization_id != organization_id:
                raise FakeCalendarConflict("cross-organization customer")
            if (start_at >= end_at
                    or (end_at - start_at).total_seconds() != template.duration_minutes * 60):
                raise FakeCalendarConflict("invalid appointment duration")
            from zoneinfo import ZoneInfo

            from safe_agent.virtual_office.timezones import checked_instant
            try:
                checked_instant(start_at, ZoneInfo(calendar.time_zone))
                checked_instant(end_at, ZoneInfo(calendar.time_zone))
                intervals = _working_intervals(calendar, start_at, end_at)
            except ValueError as error:
                raise FakeCalendarConflict("invalid calendar time") from error
            if not any(a <= start_at and end_at <= b for a, b in intervals):
                raise FakeCalendarConflict("outside working hours")
            if any(_interval_overlaps(start_at, end_at, b.start_at, b.end_at)
                   for b in calendar.bookings):
                raise FakeCalendarConflict("calendar booking conflict")
            effect_ref = "fake_event_" + sha256({
                "action_hash": action_hash, "organization_id": organization_id,
                "calendar_id": calendar_id,
            })[7:39]
            if any(b.booking_id == effect_ref for b in calendar.bookings):
                raise FakeCalendarConflict("duplicate synthetic event identifier")
            booking = PrivateBooking(
                booking_id=effect_ref, customer_id=customer_id,
                start_at=start_at, end_at=end_at,
            )
            revision = "rev_" + sha256({
                "prior": calendar.source_revision, "event": booking,
            })[7:39]
            updated = calendar.model_copy(update={
                "source_revision": revision,
                "bookings": (*calendar.bookings, booking),
            })
            self.__state = self.__state.model_copy(update={
                "calendars": tuple(
                    updated if c.calendar_id == calendar_id else c
                    for c in self.__state.calendars
                ),
            })
            self.__effects[action_hash] = effect_ref
            self.__effect_count += 1
            return effect_ref
