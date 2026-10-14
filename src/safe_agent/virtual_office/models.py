"""M1 sanitized projections/previews; separate from pinned M0 wire contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.office import AvailableSlot, Projection, SimulatedChange


class ObserveCalendar(StrictModel):
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    customer_ref: Identifier
    from_at: datetime
    until_at: datetime

    @field_validator("from_at", "until_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_window(self) -> ObserveCalendar:
        if self.until_at <= self.from_at:
            raise ValueError("empty observation window")
        if (self.until_at - self.from_at).total_seconds() > 14 * 86400:
            raise ValueError("M1 observation range cannot exceed 14 days")
        return self


class CalendarProjection(StrictModel):
    schema_version: Literal["safe_agent.calendar_projection.v1"] = (
        "safe_agent.calendar_projection.v1"
    )
    organization_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    revision: Identifier
    source_revision: Identifier
    source: Literal["synthetic_calendar"] = "synthetic_calendar"
    provenance: Literal["synthetic_fixture"] = "synthetic_fixture"
    classification: Literal["availability_only"] = "availability_only"
    allowed_fields: tuple[Literal["calendar_ref", "free_slots", "time_zone"], ...] = (
        "calendar_ref", "free_slots", "time_zone"
    )
    time_zone: str
    requested_from: datetime
    requested_until: datetime
    fetched_at: datetime
    expires_at: datetime
    free_slots: tuple[AvailableSlot, ...] = ()

    @field_validator("requested_from", "requested_until", "fetched_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("time_zone")
    @classmethod
    def recognized_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown calendar time zone") from error
        return value

    @model_validator(mode="after")
    def valid_window(self) -> CalendarProjection:
        if self.requested_from >= self.requested_until or self.fetched_at >= self.expires_at:
            raise ValueError("invalid calendar projection window")
        return self

    def to_planner_projection(self) -> Projection:
        """Deliberately downgrade to the original M0 safe planner input."""
        return Projection(
            organization_id=self.organization_id,
            task_id=self.task_id,
            calendar_ref=self.calendar_ref,
            revision=self.revision,
            fetched_at=self.fetched_at,
            expires_at=self.expires_at,
            free_slots=self.free_slots,
        )


class CalendarDiff(StrictModel):
    effect: Literal["would_create"] = "would_create"
    before: None = None
    after: SimulatedChange


CalendarConflict = Literal[
    "START_IN_PAST", "OUTSIDE_REQUEST_WINDOW", "OUTSIDE_OFFICE_HOURS",
    "BOOKING_OVERLAP", "UNKNOWN_TEMPLATE", "TEMPLATE_DURATION_MISMATCH",
    "DST_AMBIGUOUS", "DST_NONEXISTENT", "TIME_ZONE_MISMATCH",
]


class CalendarSimulation(StrictModel):
    schema_version: Literal["safe_agent.calendar_simulation.v1"] = (
        "safe_agent.calendar_simulation.v1"
    )
    simulation_ref: Identifier
    projection_rev: Identifier
    source_revision: Identifier
    change_diff: CalendarDiff
    conflicts: tuple[CalendarConflict, ...] = ()
    warnings: tuple[Literal["NOT_A_RESERVATION", "RECHECK_AT_COMMIT"], ...] = (
        "NOT_A_RESERVATION", "RECHECK_AT_COMMIT"
    )
    assumptions: tuple[Literal["SOURCE_STATE_UNCHANGED", "NO_PROVIDER_SIDE_HOLD"], ...] = (
        "SOURCE_STATE_UNCHANGED", "NO_PROVIDER_SIDE_HOLD"
    )
    expected_postconditions: tuple[Literal["event_created", "matching_time"], ...] = ()
    simulation_hash: Digest
    advisory_only: Literal[True] = True


class ObserveTasks(StrictModel):
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    customer_ref: Identifier


class SafeTaskSummary(StrictModel):
    task_ref: Identifier
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    status: Literal["open", "done"]
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class TaskListProjection(StrictModel):
    schema_version: Literal["safe_agent.task_list_projection.v1"] = (
        "safe_agent.task_list_projection.v1"
    )
    organization_id: Identifier
    task_id: Identifier
    customer_ref: Identifier
    revision: Identifier
    source_revision: Identifier
    provenance: Literal["synthetic_fixture"] = "synthetic_fixture"
    classification: Literal["task_metadata_only"] = "task_metadata_only"
    allowed_fields: tuple[Literal["task_ref", "task_code", "status", "due_at"], ...] = (
        "task_ref", "task_code", "status", "due_at"
    )
    fetched_at: datetime
    expires_at: datetime
    tasks: tuple[SafeTaskSummary, ...] = ()

    @field_validator("fetched_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class InternalTaskChange(StrictModel):
    schema_version: Literal["safe_agent.internal_task_change.v1"] = (
        "safe_agent.internal_task_change.v1"
    )
    operation: Literal["tasks.create"] = "tasks.create"
    customer_ref: Identifier
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class TaskDiff(StrictModel):
    effect: Literal["would_create"] = "would_create"
    before: None = None
    after: InternalTaskChange


TaskConflict = Literal["DUPLICATE_TASK", "DUE_DATE_NOT_FUTURE"]


class InternalTaskSimulation(StrictModel):
    schema_version: Literal["safe_agent.internal_task_simulation.v1"] = (
        "safe_agent.internal_task_simulation.v1"
    )
    simulation_ref: Identifier
    projection_rev: Identifier
    source_revision: Identifier
    change_diff: TaskDiff
    conflicts: tuple[TaskConflict, ...] = ()
    warnings: tuple[Literal["NOT_A_COMMIT"]] = ("NOT_A_COMMIT",)
    expected_postconditions: tuple[Literal["task_created"], ...] = ()
    simulation_hash: Digest
    advisory_only: Literal[True] = True
