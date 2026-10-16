"""Closed operator diagnostics and compare-and-set saved-result recovery commands."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.inference_dispatch import InvocationState
from safe_agent.evidence.ledger import OfficePhase

RecoveryStatus = Literal[
    "NOT_STARTED", "INPUT_MISSING", "INPUT_SAVED", "WAITING_TO_DISPATCH",
    "INFERENCE_PENDING", "INFERENCE_UNCERTAIN", "INFERENCE_STOPPED",
    "RESULT_RECOVERABLE", "RESULT_EXPIRED", "RESULT_MISSING", "SOURCE_CHANGED",
    "AUTHORITY_CHANGED", "POLICY_CHANGED", "PREPARED", "NEEDS_CLARIFICATION",
    "TASK_ADVANCED", "TASK_RETIRED", "BLOCKED",
]

RetirementReason = Literal[
    "NO_LONGER_NEEDED", "REPLACE_WITH_NEW_TASK", "STALE_OR_BLOCKED",
]


class PlanningRecoveryView(StrictModel):
    """Advisory status, never an execution permit or billing attestation.

    No prompt, candidate, broker handle, raw exception or balance is exposed.
    The revision identifies the inspected state; possession conveys no authority.
    """

    schema_version: Literal["safe_agent.planning_recovery.v1"] = "safe_agent.planning_recovery.v1"
    task_id: Identifier
    task_phase: OfficePhase
    task_state_version: int = Field(ge=0)
    workspace_version: int = Field(ge=0)
    attempts_used: int = Field(ge=0, le=10)
    status: RecoveryStatus
    inference_state: InvocationState | None = None
    inference_deadline: datetime | None = None
    recovery_revision: Digest
    allowed_actions: tuple[Literal["refresh", "resume_saved_result", "retire_task"], ...] = ("refresh",)
    advisory_only: Literal[True] = True
    automatic_retry_allowed: Literal[False] = False
    new_inference_requested: Literal[False] = False
    provider_effect_performed: Literal[False] = False
    billing_changed: Literal[False] = False

    @field_validator("inference_deadline")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def actions_are_bounded(self) -> PlanningRecoveryView:
        allowed = {
            ("refresh",),
            ("refresh", "retire_task"),
            ("refresh", "resume_saved_result", "retire_task"),
        }
        if self.allowed_actions not in allowed:
            raise ValueError("RECOVERY_ACTION_DENIED")
        if "resume_saved_result" in self.allowed_actions and self.status != "RESULT_RECOVERABLE":
            raise ValueError("RECOVERY_ACTION_DENIED")
        if "retire_task" in self.allowed_actions and self.status in {
            "INFERENCE_PENDING", "TASK_RETIRED", "BLOCKED",
        }:
            raise ValueError("RECOVERY_ACTION_DENIED")
        return self


class ResumeSavedResultCommand(StrictModel):
    schema_version: Literal["safe_agent.resume_saved_result.v1"] = "safe_agent.resume_saved_result.v1"
    expected_state_version: int = Field(ge=0)
    expected_workspace_version: int = Field(ge=0)
    expected_recovery_revision: Digest


class RetireTaskCommand(StrictModel):
    """Compare-and-set terminal task retirement; never a billing reconciliation."""

    schema_version: Literal["safe_agent.retire_task.v1"] = "safe_agent.retire_task.v1"
    expected_state_version: int = Field(ge=0)
    expected_workspace_version: int = Field(ge=0)
    expected_recovery_revision: Digest
    reason_code: RetirementReason
