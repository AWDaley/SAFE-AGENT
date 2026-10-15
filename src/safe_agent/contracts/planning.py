"""Versioned, privacy-minimized contracts for bounded offline planning.

Planning leases and attempt identifiers are trusted-only metadata. Public API
models never expose model output, protected references, tokens or customer data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware

PlanningMode = Literal["deterministic", "text_only"]
PlanningStatus = Literal[
    "READY", "RUNNING", "NEEDS_CLARIFICATION", "PREPARED", "EXHAUSTED",
]
PlanningResultKind = Literal[
    "PROPOSED", "NEEDS_CLARIFICATION", "RETRYABLE_FAILURE", "LEASE_EXPIRED",
]
PlanningReason = Literal[
    "NO_AVAILABLE_SLOT", "INSUFFICIENT_CONTEXT", "MODEL_RESPONSE_REJECTED",
    "SIMULATION_CONFLICT", "STALE_OR_DENIED", "LEASE_EXPIRED",
    "ATTEMPT_EXHAUSTED",
]


class PlanningCommand(StrictModel):
    schema_version: Literal["safe_agent.planning_command.v1"] = (
        "safe_agent.planning_command.v1"
    )
    expected_state_version: int = Field(ge=0)
    expected_workspace_version: int = Field(ge=0)


class PlanningLease(StrictModel):
    """Server-only, ephemeral claim for exactly one bounded planning attempt."""

    task_id: Identifier
    attempt_id: Identifier
    workspace_version: int = Field(ge=1)
    attempt_number: int = Field(ge=1, le=10)
    task_state_version: int = Field(ge=0)
    manifest_hash: Digest
    mode: PlanningMode
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class PlanningWorkspaceView(StrictModel):
    schema_version: Literal["safe_agent.planning_workspace.v1"] = (
        "safe_agent.planning_workspace.v1"
    )
    task_id: Identifier
    workspace_version: int = Field(ge=0)
    attempts_used: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=10)
    status: PlanningStatus
    mode: PlanningMode
    last_reason: PlanningReason | None = None
    proposal_hash: Digest | None = None
    synthetic_only: Literal[True] = True
    provider_effect_performed: Literal[False] = False


class PlanningReceipt(StrictModel):
    schema_version: Literal["safe_agent.planning_receipt.v1"] = (
        "safe_agent.planning_receipt.v1"
    )
    task_id: Identifier
    status: Literal["PREPARED", "NEEDS_CLARIFICATION", "READY", "EXHAUSTED"]
    workspace_version: int = Field(ge=1)
    attempts_used: int = Field(ge=1)
    max_attempts: int = Field(ge=1, le=10)
    reason_code: PlanningReason | None = None
    proposal_hash: Digest | None = None
    synthetic_only: Literal[True] = True
    provider_effect_performed: Literal[False] = False
