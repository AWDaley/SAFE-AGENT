"""W4 trusted-only immutable planning input; not an action or execution grant."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import Digest, StrictModel
from safe_agent.contracts.planning import PlanningLease
from safe_agent.virtual_office.models import CalendarProjection

MAX_PLANNING_INPUT_BYTES = 65_536


class CoordinatedPlanningInput(StrictModel):
    schema_version: Literal["safe_agent.coordinated_planning_input.v1"] = (
        "safe_agent.coordinated_planning_input.v1"
    )
    lease: PlanningLease
    policy_hash: Digest
    context: SafeTaskContext = Field(repr=False)
    projection: CalendarProjection = Field(repr=False)
    source_fingerprint: Digest = Field(repr=False)

    @model_validator(mode="after")
    def bound(self) -> CoordinatedPlanningInput:
        ctx, projection, lease = self.context, self.projection, self.lease
        if (lease.mode != "text_only" or ctx.task_id != lease.task_id
                or projection.task_id != lease.task_id
                or projection.organization_id != ctx.organization_id
                or projection.calendar_ref != ctx.calendar_ref
                or projection.time_zone != ctx.time_zone
                or projection.requested_from != ctx.allowed_start
                or projection.requested_until != ctx.allowed_end
                or ctx.simulation_ref != "simulation_pending"
                or projection.fetched_at >= lease.expires_at):
            raise ValueError("COORDINATED_INPUT_BINDING_DENIED")
        if len(canonical_json(self)) > MAX_PLANNING_INPUT_BYTES:
            raise ValueError("COORDINATED_INPUT_TOO_LARGE")
        return self

    @property
    def input_hash(self) -> str:
        return sha256(self)

    def prompt(self) -> str:
        """Transient canonical prompt; no serialized prompt is stored in SQL."""
        return canonical_json({"instruction": PLANNER_INSTRUCTION, "task": self.context,
                               "availability": self.projection.to_planner_projection()}).decode("utf-8")
