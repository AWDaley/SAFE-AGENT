"""Closed M5 test-calendar observations; no credential or provider ID crosses this boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.office import AvailableSlot


class GoogleCalendarObservation(StrictModel):
    schema_version: Literal["safe_agent.google_test_observation.v1"] = (
        "safe_agent.google_test_observation.v1"
    )
    provenance: Literal["google_test_calendar"] = "google_test_calendar"
    organization_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    connection_ref: Identifier
    revision: Digest
    fetched_at: datetime
    expires_at: datetime
    from_at: datetime
    until_at: datetime
    free_slots: tuple[AvailableSlot, ...] = ()
    advisory_only: Literal[True] = True

    @field_validator("fetched_at", "expires_at", "from_at", "until_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def window(self) -> GoogleCalendarObservation:
        if (self.fetched_at >= self.expires_at or self.from_at >= self.until_at
                or (self.until_at - self.from_at).total_seconds() > 14 * 86400):
            raise ValueError("invalid observation window")
        return self


class GoogleTestEventIntent(StrictModel):
    """Trusted only; never accept an intent unvalidated from an LLM or API client."""

    schema_version: Literal["safe_agent.google_test_intent.v1"] = (
        "safe_agent.google_test_intent.v1"
    )
    action_hash: Digest
    request_hash: Digest
    start_at: datetime
    end_at: datetime
    time_zone: str = Field(min_length=1, max_length=64)

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_interval(self) -> GoogleTestEventIntent:
        delta = (self.end_at - self.start_at).total_seconds()
        if not 300 <= delta <= 3600:
            raise ValueError("test event duration must be 5 to 60 minutes")
        try:
            zone = ZoneInfo(self.time_zone)
            from safe_agent.virtual_office.timezones import checked_instant
            checked_instant(self.start_at, zone)
            checked_instant(self.end_at, zone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("unresolvable test calendar time zone or DST offset") from error
        return self


def stable_event_id(action_hash: str) -> str:
    """Google's base32hex ID alphabet supports this SHA-256 hexadecimal subset."""
    from pydantic import TypeAdapter

    from safe_agent.contracts.base import Digest

    digest = TypeAdapter(Digest).validate_python(action_hash)
    return "safe" + digest.split(":", 1)[1][:52]
