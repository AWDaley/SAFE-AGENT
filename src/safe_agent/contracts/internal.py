"""M3 additive task-creation contracts; M0/M2 calendar wire schemas stay frozen."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.contracts.actions import (
    CalendarCreateEvent,
    RequestClarification,
    parse_proposal_json,
)
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import TaskBudget


class InternalTaskRecord(StrictModel):
    schema_version: Literal["safe_agent.internal_task_record.v1"] = "safe_agent.internal_task_record.v1"
    task_id: Identifier
    organization_id: Identifier
    initiator_id: Identifier
    manifest_id: Identifier
    manifest_hash: Digest
    objective_code: Literal["create_internal_task"] = "create_internal_task"
    created_at: datetime
    deadline: datetime

    @field_validator("created_at", "deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_deadline(self) -> InternalTaskRecord:
        if self.created_at >= self.deadline:
            raise ValueError("deadline must follow created_at")
        return self


class InternalTaskManifest(StrictModel):
    schema_version: Literal["safe_agent.internal_task_manifest.v1"] = (
        "safe_agent.internal_task_manifest.v1"
    )
    manifest_id: Identifier
    manifest_version: int = Field(ge=1)
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    customer_ref: Identifier
    permitted_operations: tuple[Literal["tasks.create"], ...] = ("tasks.create",)
    allowed_due_start: datetime
    allowed_due_end: datetime
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    requires_approval: bool = False
    budget: TaskBudget = Field(default_factory=TaskBudget)

    @field_validator("allowed_due_start", "allowed_due_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid(self) -> InternalTaskManifest:
        if self.allowed_due_start >= self.allowed_due_end:
            raise ValueError("internal task time window is empty")
        if len(set(self.permitted_operations)) != len(self.permitted_operations):
            raise ValueError("duplicate task-operation grant")
        return self


class InternalTaskCreateProposal(StrictModel):
    schema_version: Literal["safe_agent.task_proposal.v1"] = "safe_agent.task_proposal.v1"
    operation: Literal["tasks.create"] = "tasks.create"
    customer_ref: Identifier
    task_code: Literal["call_back", "prepare_documents", "review_request"]
    due_at: datetime
    simulation_ref: Identifier
    projection_rev: Identifier

    @field_validator("due_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


def parse_office_proposal_json(
    payload: str,
) -> CalendarCreateEvent | InternalTaskCreateProposal | RequestClarification:
    """Single closed office action; preserves M0's original calendar parser/schema."""
    from safe_agent.contracts.actions import MAX_PROPOSAL_BYTES, _forbid_nonfinite, _unique_pairs
    if len(payload.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValueError("proposal exceeds size limit")
    raw = json.loads(payload, object_pairs_hook=_unique_pairs, parse_constant=_forbid_nonfinite)
    if not isinstance(raw, dict):
        raise ValueError("one proposal object required")
    if raw.get("operation") == "tasks.create":
        return InternalTaskCreateProposal.model_validate_json(json.dumps(raw, allow_nan=False))
    return parse_proposal_json(payload)
