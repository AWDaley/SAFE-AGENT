"""Trusted task, manifest, reference, approval and evidence *types*.

These schemas do not create permissions. TAL must compile and authorize the
manifest in a later integration milestone.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from .base import Digest, Identifier, StrictModel, require_aware


class TaskState(StrEnum):
    ASSIGNED = "ASSIGNED"
    PLANNING = "PLANNING"
    PROPOSED_UNVERIFIED = "PROPOSED_UNVERIFIED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    RETIRED = "RETIRED"


class TaskBudget(StrictModel):
    schema_version: Literal["safe_agent.budget.v1"] = "safe_agent.budget.v1"
    max_cycles: int = Field(default=4, ge=1, le=100)
    max_external_effects: int = Field(default=1, ge=0, le=10)
    max_effects_per_cycle: Literal[1] = 1


class TaskManifest(StrictModel):
    schema_version: Literal["safe_agent.manifest.v1"] = "safe_agent.manifest.v1"
    manifest_id: Identifier
    manifest_version: int = Field(ge=1)
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    customer_ref: Identifier
    permitted_operations: tuple[Literal["calendar.create_event"], ...]
    allowed_start: datetime
    allowed_end: datetime
    max_duration_minutes: int = Field(default=60, ge=1, le=60)
    time_zone: str
    template_id: Identifier
    requires_approval: bool = False
    budget: TaskBudget = Field(default_factory=TaskBudget)

    @field_validator("allowed_start", "allowed_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("time_zone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown IANA time zone") from error
        return value

    @model_validator(mode="after")
    def valid_window(self) -> TaskManifest:
        if self.allowed_start >= self.allowed_end:
            raise ValueError("allowed_start must be before allowed_end")
        if len(set(self.permitted_operations)) != len(self.permitted_operations):
            raise ValueError("duplicate operation grants")
        return self


class Task(StrictModel):
    schema_version: Literal["safe_agent.task.v1"] = "safe_agent.task.v1"
    task_id: Identifier
    organization_id: Identifier
    initiator_id: Identifier
    manifest_id: Identifier
    manifest_hash: Digest
    objective_code: Literal["schedule_follow_up"]
    state: TaskState = TaskState.ASSIGNED
    created_at: datetime
    deadline: datetime

    @field_validator("created_at", "deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_deadline(self) -> Task:
        if self.created_at >= self.deadline:
            raise ValueError("deadline must follow created_at")
        return self


class ScopedReference(StrictModel):
    """TRUSTED ONLY: handle is not a bearer capability. Resource ID stays broker-side."""

    schema_version: Literal["safe_agent.scoped_ref.v1"] = "safe_agent.scoped_ref.v1"
    handle: Identifier
    organization_id: Identifier
    task_id: Identifier
    initiator_id: Identifier
    resource_binding_id: Identifier
    permitted_operations: tuple[Literal["calendar.create_event"], ...]
    expires_at: datetime
    revoked: bool = False

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class DecisionStatus(StrEnum):
    DENIED = "DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AUTHORIZED = "AUTHORIZED"


class ActionDecision(StrictModel):
    schema_version: Literal["safe_agent.decision.v1"] = "safe_agent.decision.v1"
    decision: DecisionStatus
    manifest_hash: Digest
    proposal_hash: Digest
    reason_code: Identifier
    state_version: int = Field(ge=0)


class Approval(StrictModel):
    schema_version: Literal["safe_agent.approval.v1"] = "safe_agent.approval.v1"
    approval_id: Identifier
    task_id: Identifier
    approver_id: Identifier
    proposal_hash: Digest
    manifest_hash: Digest
    expires_at: datetime
    approved: bool

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class ExecutionOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ExecutionReceipt(StrictModel):
    schema_version: Literal["safe_agent.receipt.v1"] = "safe_agent.receipt.v1"
    action_hash: Digest
    manifest_hash: Digest
    outcome: ExecutionOutcome
    effect_ref: Identifier | None = None
    verification_ref: Identifier | None = None


class EvidenceEvent(StrictModel):
    """Opaque payload reference prevents model-visible logs from embedding customer data."""

    schema_version: Literal["safe_agent.evidence.v1"] = "safe_agent.evidence.v1"
    event_id: Identifier
    task_id: Identifier
    ledger_sequence: int = Field(ge=1)
    event_kind: Literal["PROPOSED", "DENIED", "DISPATCHED", "VERIFIED", "OUTCOME_UNKNOWN"]
    occurred_at: datetime
    previous_event_hash: Digest | None = None
    payload_digest: Digest

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
