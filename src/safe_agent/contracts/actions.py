"""Exactly one model proposal per parse; no generic shell, network, SQL or email."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .base import Identifier, StrictModel, require_aware


class CalendarCreateEvent(StrictModel):
    schema_version: Literal["safe_agent.proposal.v1"] = "safe_agent.proposal.v1"
    operation: Literal["calendar.create_event"] = "calendar.create_event"
    calendar_ref: Identifier
    customer_ref: Identifier
    start_at: datetime
    end_at: datetime
    time_zone: str = Field(min_length=1, max_length=64)
    template_id: Identifier
    simulation_ref: Identifier
    projection_rev: Identifier

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_times(self) -> CalendarCreateEvent:
        if self.start_at >= self.end_at:
            raise ValueError("start_at must precede end_at")
        return self


class RequestClarification(StrictModel):
    schema_version: Literal["safe_agent.proposal.v1"] = "safe_agent.proposal.v1"
    operation: Literal["agent.request_clarification"] = "agent.request_clarification"
    reason_code: Literal["NO_AVAILABLE_SLOT", "INSUFFICIENT_CONTEXT"]
    question: str = Field(min_length=1, max_length=256)


Proposal = Annotated[CalendarCreateEvent | RequestClarification, Field(discriminator="operation")]
_PROPOSAL_ADAPTER = TypeAdapter(Proposal)
MAX_PROPOSAL_BYTES = 16_384


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _forbid_nonfinite(token: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {token}")


def parse_proposal_json(payload: str) -> CalendarCreateEvent | RequestClarification:
    """Reject duplicate keys, multi-action lists, non-JSON constants, and extra fields."""

    if len(payload.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValueError("proposal exceeds size limit")
    raw = json.loads(payload, object_pairs_hook=_unique_pairs, parse_constant=_forbid_nonfinite)
    if not isinstance(raw, dict):
        raise ValueError("exactly one JSON object is required")
    # validate_json interprets datetime strings as JSON timestamps even under
    # strict Pydantic mode, without relaxing schema closure or numeric types.
    return _PROPOSAL_ADAPTER.validate_json(json.dumps(raw, allow_nan=False))
