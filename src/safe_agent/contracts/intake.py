"""M6 trusted synthetic task-intake contracts; no model-created authority."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from safe_agent.contracts.base import Identifier, StrictModel, require_aware
from safe_agent.evidence.ledger import OfficePhase

Workflow = Literal["schedule_follow_up", "create_internal_task"]
RequestKey = Annotated[str, StringConstraints(pattern=r"^req_[0-9a-f]{32}$")]


class TaskIntakeRequest(StrictModel):
    schema_version: Literal["safe_agent.task_intake.v1"] = "safe_agent.task_intake.v1"
    option_id: Identifier
    idempotency_key: RequestKey
    allowed_start: datetime
    allowed_end: datetime

    @field_validator("allowed_start", "allowed_end", mode="before")
    @classmethod
    def aware(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise ValueError("offset-aware datetime required")
        return require_aware(value)

    @model_validator(mode="after")
    def valid_window(self) -> TaskIntakeRequest:
        if self.allowed_start >= self.allowed_end:
            raise ValueError("task window is empty")
        return self


class TaskOptionView(StrictModel):
    option_id: Identifier
    workflow: Workflow
    display_name: Literal["Schedule follow-up", "Create internal task"]
    time_zone: str
    requires_approval: bool
    max_ahead_days: int = Field(ge=1, le=30)
    max_window_minutes: int = Field(ge=15, le=480)


class TaskOptionsPage(StrictModel):
    schema_version: Literal["safe_agent.task_options.v1"] = "safe_agent.task_options.v1"
    options: tuple[TaskOptionView, ...]


class TaskIntakeReceipt(StrictModel):
    schema_version: Literal["safe_agent.task_intake_receipt.v1"] = (
        "safe_agent.task_intake_receipt.v1"
    )
    task_id: Identifier
    phase: OfficePhase
    state_version: int = Field(ge=0)
    synthetic_only: Literal[True] = True
    provider_write_authorized: Literal[False] = False
