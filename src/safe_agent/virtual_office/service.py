"""Deterministic, read-only projections and calendar/internal-task simulations.

The private source snapshot and reference resolver are never passed to the
planner. No method here can commit an external or internal business effect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.office import AvailableSlot, SimulatedChange
from safe_agent.virtual_office.models import (
    CalendarConflict,
    CalendarDiff,
    CalendarProjection,
    CalendarSimulation,
    InternalTaskChange,
    InternalTaskSimulation,
    ObserveCalendar,
    ObserveTasks,
    SafeTaskSummary,
    TaskConflict,
    TaskDiff,
    TaskListProjection,
)
from safe_agent.virtual_office.state import PrivateCalendar, SyntheticOfficeState
from safe_agent.virtual_office.timezones import (
    CalendarTimeError,
    checked_instant,
    checked_local_time,
)


class UnissuedProjection(ValueError):
    """Projections must originate from a trusted, intact observation."""


class StaleProjection(ValueError):
    """A projection is expired or no longer matches the synthetic source."""


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)


def _interval_overlaps(a_start: datetime, a_end: datetime,
                       b_start: datetime, b_end: datetime) -> bool:
    # Half-open [start, end): back-to-back appointments do not overlap.
    return a_start < b_end and b_start < a_end


def _working_intervals(
    calendar: PrivateCalendar, from_at: datetime, until_at: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    """Generate clipped local work intervals; reject unresolvable DST boundaries."""
    zone = ZoneInfo(calendar.time_zone)
    first = from_at.astimezone(zone).date()
    last = until_at.astimezone(zone).date()
    schedule = {hours.weekday: hours for hours in calendar.office_hours}
    current = first
    intervals: list[tuple[datetime, datetime]] = []
    while current <= last:
        hours = schedule.get(current.weekday())
        if hours is not None:
            start = checked_local_time(current, hours.opens, zone)
            end = checked_local_time(current, hours.closes, zone)
            lower, upper = max(start, from_at), min(end, until_at)
            if lower < upper:
                intervals.append((lower, upper))
        current += timedelta(days=1)
    return tuple(intervals)


def _available_slots(
    calendar: PrivateCalendar, from_at: datetime, until_at: datetime
) -> tuple[AvailableSlot, ...]:
    intervals = _working_intervals(calendar, from_at, until_at)
    result: list[AvailableSlot] = []
    bookings = sorted(calendar.bookings, key=lambda b: (_utc(b.start_at), b.booking_id))
    for start, end in intervals:
        cursor = start
        for booking in bookings:
            if not _interval_overlaps(start, end, booking.start_at, booking.end_at):
                continue
            busy_start = max(start, booking.start_at)
            busy_end = min(end, booking.end_at)
            if cursor < busy_start:
                result.append(AvailableSlot(start_at=cursor, end_at=busy_start))
            cursor = max(cursor, busy_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append(AvailableSlot(start_at=cursor, end_at=end))
    return tuple(sorted(result, key=lambda slot: _utc(slot.start_at)))


def _short_id(prefix: str, payload: object) -> str:
    return prefix + "_" + sha256(payload).split(":", 1)[1][:30]


class VirtualOffice:
    """Synthetic-only Virtual Office, separate from TAL and external connectors.

    `replace_synthetic_source` is a trusted test hook for simulating syncs from
    a changing source. `simulate` and `simulate_task` never change that source.
    """

    def __init__(self, source: SyntheticOfficeState, broker: ReferenceBroker,
                 *, freshness: timedelta = timedelta(minutes=10)) -> None:
        if not timedelta(0) < freshness <= timedelta(hours=1):
            raise ValueError("freshness must be positive and at most one hour")
        self._source = source
        self._broker = broker
        self._freshness = freshness
        self._observations: dict[str, CalendarProjection] = {}
        self._source_fingerprints: dict[str, str] = {}
        self._task_observations: dict[str, TaskListProjection] = {}
        self._task_fingerprints: dict[str, str] = {}

    def replace_synthetic_source(self, new_source: SyntheticOfficeState) -> None:
        """Trusted synthetic sync hook; never available to the agent/planner."""
        if new_source.organization_id != self._source.organization_id:
            raise ValueError("cross-organization synthetic snapshot")
        self._source = new_source

    def trusted_template_duration(self, template_id: str, *, organization_id: str) -> int:
        """Resolve a fixture-only template duration for the trusted orchestrator.

        Never expose the synthetic source, customer metadata or this resolver
        to the model; its planning input is the sanitized projection.
        """
        if self._source.organization_id != organization_id:
            raise ValueError("synthetic organization mismatch")
        return self._source.template(template_id).duration_minutes

    def _resolve(self, handle: str, *, tenant: str, actor: str, task: str,
                 operation: str, kind: ResourceKind, now: datetime) -> str:
        # Operation type narrowed by the caller to the two M1 literal operations.
        if operation not in ("calendar.create_event", "tasks.create"):
            raise ValueError("unsupported synthetic operation")
        scope = BrokerScope.model_validate({
            "organization_id": tenant,
            "initiator_id": actor,
            "task_id": task,
            "operation": operation,
            "now": now,
        })
        return self._broker.resolve(handle, scope=scope, kind=kind)

    def observe(self, request: ObserveCalendar, *, as_of: datetime) -> CalendarProjection:
        """Observe sanitized free/busy only; private booking details never leave trust layer."""
        as_of = require_aware(as_of)
        if request.organization_id != self._source.organization_id:
            raise ValueError("organization mismatch")
        calendar_id = self._resolve(
            request.calendar_ref, tenant=request.organization_id,
            actor=request.initiator_id, task=request.task_id,
            operation="calendar.create_event", kind=ResourceKind.CALENDAR, now=as_of,
        )
        customer_id = self._resolve(
            request.customer_ref, tenant=request.organization_id,
            actor=request.initiator_id, task=request.task_id,
            operation="calendar.create_event", kind=ResourceKind.CUSTOMER, now=as_of,
        )
        # Fail closed if a broker was seeded with a foreign or nonexistent record.
        calendar = self._source.calendar(calendar_id)
        customer = self._source.customer(customer_id)
        if (calendar.organization_id != request.organization_id
                or customer.organization_id != request.organization_id):
            raise ValueError("resource organization mismatch")
        free = _available_slots(calendar, request.from_at, request.until_at)
        expires = as_of + self._freshness
        revision = _short_id("projection", {
            "task_id": request.task_id,
            "organization_id": request.organization_id,
            "calendar_ref": request.calendar_ref,
            "customer_ref": request.customer_ref,
            "source_revision": calendar.source_revision,
            "requested_from": request.from_at,
            "requested_until": request.until_at,
            "fetched_at": as_of,
            "expires_at": expires,
            "free_slots": free,
        })
        projection = CalendarProjection(
            organization_id=request.organization_id,
            task_id=request.task_id,
            calendar_ref=request.calendar_ref,
            revision=revision,
            source_revision=calendar.source_revision,
            time_zone=calendar.time_zone,
            requested_from=request.from_at,
            requested_until=request.until_at,
            fetched_at=as_of,
            expires_at=expires,
            free_slots=free,
        )
        self._observations[revision] = projection
        self._source_fingerprints[revision] = sha256(calendar)
        return projection

    def _check_projection(self, projection: CalendarProjection, *, now: datetime) -> None:
        if self._observations.get(projection.revision) != projection:
            raise UnissuedProjection("unrecognized or altered calendar projection")
        if now < projection.fetched_at or now >= projection.expires_at:
            raise StaleProjection("calendar projection expired")

    def simulate(self, projection: CalendarProjection, change: SimulatedChange,
                 *, actor: str, customer_ref: str, now: datetime) -> CalendarSimulation:
        """Pure preview. Never performs an authoritative calendar mutation."""
        now = require_aware(now)
        self._check_projection(projection, now=now)
        if change.calendar_ref != projection.calendar_ref or change.customer_ref != customer_ref:
            raise ValueError("change does not match scoped projection")
        if change.end_at <= change.start_at:
            raise ValueError("invalid simulated calendar interval")
        calendar_id = self._resolve(
            projection.calendar_ref, tenant=projection.organization_id, actor=actor,
            task=projection.task_id, operation="calendar.create_event",
            kind=ResourceKind.CALENDAR, now=now,
        )
        customer_id = self._resolve(
            change.customer_ref, tenant=projection.organization_id, actor=actor,
            task=projection.task_id, operation="calendar.create_event",
            kind=ResourceKind.CUSTOMER, now=now,
        )
        calendar = self._source.calendar(calendar_id)
        customer = self._source.customer(customer_id)
        if customer.organization_id != projection.organization_id:
            raise ValueError("cross-tenant customer record")
        if (calendar.source_revision != projection.source_revision
                or sha256(calendar) != self._source_fingerprints[projection.revision]):
            raise StaleProjection("calendar source revision or content changed")

        conflicts: list[CalendarConflict] = []
        if change.start_at <= now:
            conflicts.append("START_IN_PAST")
        if change.start_at < projection.requested_from or change.end_at > projection.requested_until:
            conflicts.append("OUTSIDE_REQUEST_WINDOW")
        zone = ZoneInfo(calendar.time_zone)
        timezone_valid = True
        for point in (change.start_at, change.end_at):
            try:
                checked_instant(point, zone)
            except CalendarTimeError as error:
                timezone_valid = False
                msg = str(error)
                code: CalendarConflict
                if "ambiguous" in msg:
                    code = "DST_AMBIGUOUS"
                elif "nonexistent" in msg:
                    code = "DST_NONEXISTENT"
                else:
                    code = "TIME_ZONE_MISMATCH"
                if code not in conflicts:
                    conflicts.append(code)
        # When the offset is invalid, computing interval membership is still safe
        # but calling it a confirmed office-hours match would be misleading.
        if timezone_valid:
            # Calendar-local work schedule. Candidate must fit one contiguous shift.
            work = _working_intervals(calendar, change.start_at, change.end_at)
            if not any(start <= change.start_at and change.end_at <= end
                       for start, end in work):
                conflicts.append("OUTSIDE_OFFICE_HOURS")
        if any(_interval_overlaps(change.start_at, change.end_at, b.start_at, b.end_at)
               for b in calendar.bookings):
            conflicts.append("BOOKING_OVERLAP")
        template = next((t for t in self._source.appointment_templates
                         if t.template_id == change.template_id), None)
        if template is None:
            conflicts.append("UNKNOWN_TEMPLATE")
        elif (change.end_at - change.start_at) != timedelta(minutes=template.duration_minutes):
            conflicts.append("TEMPLATE_DURATION_MISMATCH")

        unique_conflicts = tuple(sorted(set(conflicts)))
        change_diff = CalendarDiff(after=change)
        preimage = {
            "projection_rev": projection.revision,
            "source_revision": projection.source_revision,
            "change_diff": change_diff,
            "conflicts": unique_conflicts,
            "expected_postconditions": ("event_created", "matching_time")
            if not unique_conflicts else (),
            "warnings": ("NOT_A_RESERVATION", "RECHECK_AT_COMMIT"),
            "assumptions": ("SOURCE_STATE_UNCHANGED", "NO_PROVIDER_SIDE_HOLD"),
        }
        simulation_ref = _short_id("simulation", preimage)
        return CalendarSimulation(
            simulation_ref=simulation_ref,
            projection_rev=projection.revision,
            source_revision=projection.source_revision,
            change_diff=change_diff,
            conflicts=unique_conflicts,
            expected_postconditions=("event_created", "matching_time")
            if not unique_conflicts else (),
            simulation_hash=sha256({**preimage, "simulation_ref": simulation_ref}),
        )

    def observe_tasks(self, request: ObserveTasks, *, as_of: datetime) -> TaskListProjection:
        as_of = require_aware(as_of)
        if request.organization_id != self._source.organization_id:
            raise ValueError("organization mismatch")
        customer_id = self._resolve(
            request.customer_ref, tenant=request.organization_id,
            actor=request.initiator_id, task=request.task_id,
            operation="tasks.create", kind=ResourceKind.CUSTOMER, now=as_of,
        )
        customer = self._source.customer(customer_id)
        if customer.organization_id != request.organization_id:
            raise ValueError("cross-tenant customer")
        # Scoped broker handles for observed internal tasks. These are not
        # grants to modify a task; M1 supports creation previews only.
        scope = BrokerScope(
            organization_id=request.organization_id,
            initiator_id=request.initiator_id,
            task_id=request.task_id,
            operation="tasks.create",
            now=as_of,
        )
        tasks = tuple(sorted((
            SafeTaskSummary(
                task_ref=self._broker.mint(
                    resource_id=t.task_id,
                    resource_kind=ResourceKind.INTERNAL_TASK,
                    scope=scope,
                    permitted_operations=("tasks.create",),
                    expires_at=as_of + self._freshness,
                ),
                task_code=t.task_code,
                status=t.status,
                due_at=t.due_at,
            )
            for t in self._source.internal_tasks if t.customer_id == customer_id
        ), key=lambda x: (x.due_at, x.task_ref)))
        expires = as_of + self._freshness
        revision = _short_id("task_projection", {
            "organization_id": request.organization_id,
            "task_id": request.task_id,
            "customer_ref": request.customer_ref,
            "source_revision": self._source.task_list_revision,
            "fetched_at": as_of,
            "expires_at": expires,
            "tasks": tasks,
        })
        projection = TaskListProjection(
            organization_id=request.organization_id,
            task_id=request.task_id,
            customer_ref=request.customer_ref,
            revision=revision,
            source_revision=self._source.task_list_revision,
            fetched_at=as_of,
            expires_at=expires,
            tasks=tasks,
        )
        self._task_observations[revision] = projection
        self._task_fingerprints[revision] = sha256(self._source.internal_tasks)
        return projection

    def simulate_task(self, projection: TaskListProjection, change: InternalTaskChange,
                      *, actor: str, now: datetime) -> InternalTaskSimulation:
        """M1 internal-task creation preview; does not create even an internal task."""
        now = require_aware(now)
        if self._task_observations.get(projection.revision) != projection:
            raise UnissuedProjection("unrecognized or altered task projection")
        if now < projection.fetched_at or now >= projection.expires_at:
            raise StaleProjection("task projection expired")
        if (projection.source_revision != self._source.task_list_revision
                or sha256(self._source.internal_tasks)
                != self._task_fingerprints[projection.revision]):
            raise StaleProjection("task source revision or content changed")
        if change.customer_ref != projection.customer_ref:
            raise ValueError("customer scope mismatch")
        customer_id = self._resolve(
            change.customer_ref, tenant=projection.organization_id,
            actor=actor, task=projection.task_id, operation="tasks.create",
            kind=ResourceKind.CUSTOMER, now=now,
        )
        conflicts: list[TaskConflict] = []
        if change.due_at <= now:
            conflicts.append("DUE_DATE_NOT_FUTURE")
        if any(t.customer_id == customer_id and t.task_code == change.task_code
               and _utc(t.due_at) == _utc(change.due_at)
               for t in self._source.internal_tasks):
            conflicts.append("DUPLICATE_TASK")
        unique_conflicts = tuple(sorted(conflicts))
        preimage = {
            "projection_rev": projection.revision,
            "source_revision": projection.source_revision,
            "change_diff": TaskDiff(after=change),
            "conflicts": unique_conflicts,
            "expected_postconditions": ("task_created",) if not unique_conflicts else (),
            "warnings": ("NOT_A_COMMIT",),
        }
        ref = _short_id("simulation", preimage)
        return InternalTaskSimulation(
            simulation_ref=ref,
            projection_rev=projection.revision,
            source_revision=projection.source_revision,
            change_diff=TaskDiff(after=change),
            conflicts=unique_conflicts,
            expected_postconditions=("task_created",) if not unique_conflicts else (),
            simulation_hash=sha256({**preimage, "simulation_ref": ref}),
        )

    def trusted_projection_fingerprint(self, projection: CalendarProjection, *, now: datetime) -> str:
        """Internal precommit evidence; never exposes the source's private record fields.

        The gateway must also compare against the *current fake provider read*, not
        merely this cache, because the authoritative fake source may have changed.
        """
        self._check_projection(projection, now=require_aware(now))
        return self._source_fingerprints[projection.revision]


    def trusted_task_projection_fingerprint(self, projection: TaskListProjection,
                                           *, now: datetime) -> str:
        if self._task_observations.get(projection.revision) != projection:
            raise UnissuedProjection("unrecognized or altered task projection")
        if now < projection.fetched_at or now >= projection.expires_at:
            raise StaleProjection("task projection expired")
        return self._task_fingerprints[projection.revision]
