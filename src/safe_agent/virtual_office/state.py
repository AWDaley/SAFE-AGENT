"""TRUSTED ONLY. Synthetic business records are not passed to the agent.

Fixtures intentionally contain synthetic names and emails to prove that the
sanitized projections never serialize private record fields to the model.
"""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from safe_agent.contracts.base import Identifier, StrictModel, require_aware


class OfficeHours(StrictModel):
    weekday: int = Field(ge=0, le=6)
    opens: time
    closes: time

    @model_validator(mode="after")
    def valid_hours(self) -> OfficeHours:
        if self.opens.tzinfo or self.closes.tzinfo or self.opens >= self.closes:
            raise ValueError("working hours must be ordered local wall-clock times")
        return self


class PrivateBooking(StrictModel):
    booking_id: Identifier
    customer_id: Identifier
    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_interval(self) -> PrivateBooking:
        if self.start_at >= self.end_at:
            raise ValueError("invalid booking interval")
        return self


class PrivateCalendar(StrictModel):
    calendar_id: Identifier
    organization_id: Identifier
    time_zone: str
    source_revision: Identifier
    office_hours: tuple[OfficeHours, ...]
    bookings: tuple[PrivateBooking, ...] = ()

    @field_validator("time_zone")
    @classmethod
    def zone_is_known(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown IANA time zone") from error
        return value

    @model_validator(mode="after")
    def no_duplicate_hours(self) -> PrivateCalendar:
        if len(set(x.weekday for x in self.office_hours)) != len(self.office_hours):
            raise ValueError("one office-hours interval per weekday for M1")
        ids = [x.booking_id for x in self.bookings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate booking identifier")
        return self


class PrivateCustomer(StrictModel):
    customer_id: Identifier
    organization_id: Identifier
    name: str = Field(min_length=1)
    email: str | None = None


class AppointmentTemplate(StrictModel):
    template_id: Identifier
    duration_minutes: int = Field(ge=5, le=120)


class PrivateInternalTask(StrictModel):
    task_id: Identifier
    organization_id: Identifier
    customer_id: Identifier
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    status: Literal["open", "done"]
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class SyntheticOfficeState(StrictModel):
    schema_version: Literal["safe_agent.synthetic_office.v1"] = "safe_agent.synthetic_office.v1"
    organization_id: Identifier
    task_list_revision: Identifier
    calendars: tuple[PrivateCalendar, ...]
    customers: tuple[PrivateCustomer, ...]
    appointment_templates: tuple[AppointmentTemplate, ...]
    internal_tasks: tuple[PrivateInternalTask, ...] = ()

    @model_validator(mode="after")
    def validate_relations(self) -> SyntheticOfficeState:
        if len(self.calendars) < 1 or len(self.customers) < 1:
            raise ValueError("synthetic state requires calendars and customers")
        for group, key in [
            (self.calendars, "calendar_id"),
            (self.customers, "customer_id"),
            (self.appointment_templates, "template_id"),
            (self.internal_tasks, "task_id"),
        ]:
            ids = [getattr(x, key) for x in group]
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {key}")
        customer_ids = {c.customer_id for c in self.customers}
        if any(c.organization_id != self.organization_id for c in self.customers):
            raise ValueError("cross-tenant customer record")
        if any(c.organization_id != self.organization_id for c in self.calendars):
            raise ValueError("cross-tenant calendar record")
        if any(t.organization_id != self.organization_id for t in self.internal_tasks):
            raise ValueError("cross-tenant internal task record")
        if any(b.customer_id not in customer_ids for c in self.calendars for b in c.bookings):
            raise ValueError("booking refers to missing customer")
        if any(t.customer_id not in customer_ids for t in self.internal_tasks):
            raise ValueError("internal task refers to missing customer")
        return self

    @classmethod
    def from_fixture(cls, path: Path) -> SyntheticOfficeState:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def calendar(self, calendar_id: str) -> PrivateCalendar:
        return next(c for c in self.calendars if c.calendar_id == calendar_id)

    def customer(self, customer_id: str) -> PrivateCustomer:
        return next(c for c in self.customers if c.customer_id == customer_id)

    def template(self, template_id: str) -> AppointmentTemplate:
        return next(t for t in self.appointment_templates if t.template_id == template_id)
