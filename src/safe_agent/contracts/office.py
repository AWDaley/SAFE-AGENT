"""Side-effect-free, sanitized Virtual Office projection and simulation types."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .base import Digest, Identifier, StrictModel, require_aware


class AvailableSlot(StrictModel):
    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_interval(self) -> AvailableSlot:
        if self.start_at >= self.end_at:
            raise ValueError("start_at must precede end_at")
        return self


class Projection(StrictModel):
    """Model-visible projection contains no customer name, email, or provider ID."""

    schema_version: Literal["safe_agent.projection.v1"] = "safe_agent.projection.v1"
    organization_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    revision: Identifier
    source: Literal["synthetic_calendar"] = "synthetic_calendar"
    fetched_at: datetime
    expires_at: datetime
    free_slots: tuple[AvailableSlot, ...] = ()

    @field_validator("fetched_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_freshness(self) -> Projection:
        if self.fetched_at >= self.expires_at:
            raise ValueError("expires_at must follow fetched_at")
        return self


class SimulatedChange(StrictModel):
    operation: Literal["calendar.create_event"] = "calendar.create_event"
    calendar_ref: Identifier
    customer_ref: Identifier
    start_at: datetime
    end_at: datetime
    template_id: Identifier

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class Simulation(StrictModel):
    schema_version: Literal["safe_agent.simulation.v1"] = "safe_agent.simulation.v1"
    simulation_ref: Identifier
    projection_rev: Identifier
    change: SimulatedChange
    conflicts: tuple[Identifier, ...] = ()
    expected_postconditions: tuple[Literal["event_created", "matching_time"], ...] = ()
    simulation_hash: Digest
    advisory_only: Literal[True] = True
    # No provider-side hold, no authoritative commit, no credentials.
