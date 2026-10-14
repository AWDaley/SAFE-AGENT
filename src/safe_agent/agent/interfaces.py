"""Unprivileged planner protocol and minimized planning inputs.

The model worker must not receive a tool registry, credential, reference resolver,
or external-write interface. The orchestration layer constructs these inputs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import field_validator, model_validator

from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.base import Identifier, StrictModel, require_aware
from safe_agent.contracts.office import Projection


class SafeTaskContext(StrictModel):
    schema_version: Literal["safe_agent.planner_context.v1"] = "safe_agent.planner_context.v1"
    task_id: Identifier
    organization_id: Identifier
    calendar_ref: Identifier
    customer_ref: Identifier
    allowed_start: datetime
    allowed_end: datetime
    duration_minutes: int
    time_zone: str
    template_id: Identifier
    simulation_ref: Identifier

    @field_validator("allowed_start", "allowed_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_window(self) -> SafeTaskContext:
        if self.allowed_start >= self.allowed_end:
            raise ValueError("empty scheduling window")
        if not 1 <= self.duration_minutes <= 60:
            raise ValueError("duration out of M0 bounds")
        return self


class Planner(Protocol):
    def propose(
        self, context: SafeTaskContext, projection: Projection
    ) -> CalendarCreateEvent | RequestClarification: ...
