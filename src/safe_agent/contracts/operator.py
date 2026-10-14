"""Closed, sanitized operator inbox and proposal-review contracts.

These DTOs contain only opaque task-scoped handles and fixed codes. Neither
raw provider identifiers nor customer data nor OAuth material may be exposed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.evidence.ledger import OfficePhase


class OperatorInboxItem(StrictModel):
    schema_version: Literal["safe_agent.operator_inbox_item.v1"] = (
        "safe_agent.operator_inbox_item.v1"
    )
    task_id: Identifier
    phase: OfficePhase
    state_version: int = Field(ge=0)
    has_proposal: bool
    approval_pending: bool


class OperatorInboxPage(StrictModel):
    schema_version: Literal["safe_agent.operator_inbox_page.v1"] = (
        "safe_agent.operator_inbox_page.v1"
    )
    items: tuple[OperatorInboxItem, ...]
    next_cursor: Identifier | None = None


class CalendarOperatorPreview(StrictModel):
    schema_version: Literal["safe_agent.operator_calendar_preview.v1"] = (
        "safe_agent.operator_calendar_preview.v1"
    )
    operation: Literal["calendar.create_event"] = "calendar.create_event"
    task_id: Identifier
    proposal_hash: Digest
    state_version: int = Field(ge=0)
    phase: OfficePhase
    calendar_ref: Identifier
    customer_ref: Identifier
    template_id: Identifier
    start_at: datetime
    end_at: datetime
    time_zone: str
    approval_required: bool
    advisory_only: Literal[True] = True
    source: Literal["synthetic_preview"] = "synthetic_preview"
    reserves_slot: Literal[False] = False

    @field_validator("start_at", "end_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class InternalTaskOperatorPreview(StrictModel):
    schema_version: Literal["safe_agent.operator_internal_preview.v1"] = (
        "safe_agent.operator_internal_preview.v1"
    )
    operation: Literal["tasks.create"] = "tasks.create"
    task_id: Identifier
    proposal_hash: Digest
    state_version: int = Field(ge=0)
    phase: OfficePhase
    customer_ref: Identifier
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    due_at: datetime
    approval_required: bool
    advisory_only: Literal[True] = True
    source: Literal["synthetic_preview"] = "synthetic_preview"

    @field_validator("due_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


OperatorProposalPreview = CalendarOperatorPreview | InternalTaskOperatorPreview
