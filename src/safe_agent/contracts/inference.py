"""M7-01 inference-only contracts: no endpoint, secrets, grants, or execution tools.

A transport reply is untrusted model data. Neither this schema nor a provider
usage report is a verified billing record or a TAL execution authorization.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.office import Projection

InferenceRequestId = Annotated[str, StringConstraints(pattern=r"^inf_[0-9a-f]{32}$")]

OperationClass = Literal["calendar.propose_or_clarify.v1"]


class InferenceRoutePolicy(StrictModel):
    """Private, trusted-bootstrap provider-neutral route allowlist.

    Model route metadata has no URL, API key, transport command, secret handle,
    tool schema, or operator-editable execution permission field.
    """

    schema_version: Literal["safe_agent.inference_route_policy.v1"] = (
        "safe_agent.inference_route_policy.v1"
    )
    route_id: Identifier
    model_revision: Identifier
    operation_class: OperationClass = "calendar.propose_or_clarify.v1"
    max_prompt_bytes: int = Field(default=8192, ge=512, le=16_384)
    max_response_bytes: int = Field(default=2048, ge=256, le=16_384)
    max_prompt_token_proxy: int = Field(default=8192, ge=512, le=16_384)
    max_response_token_proxy: int = Field(default=2048, ge=256, le=16_384)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    max_estimated_cost_microusd: int = Field(default=0, ge=0, le=10_000_000)
    input_microusd_per_million_tokens: int = Field(
        default=0, ge=0, le=1_000_000_000,
    )
    output_microusd_per_million_tokens: int = Field(
        default=0, ge=0, le=1_000_000_000,
    )


class InferenceRequest(StrictModel):
    """One issued, minimized inference call, scoped to an immutable projection."""

    schema_version: Literal["safe_agent.inference_request.v1"] = (
        "safe_agent.inference_request.v1"
    )
    request_id: InferenceRequestId
    route_id: Identifier
    model_revision: Identifier
    operation_class: OperationClass = "calendar.propose_or_clarify.v1"
    prompt_digest: Digest
    issued_at: datetime
    deadline_at: datetime
    task: SafeTaskContext
    availability: Projection

    @field_validator("issued_at", "deadline_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bound_to_projection(self) -> InferenceRequest:
        if (self.task.task_id != self.availability.task_id
                or self.task.organization_id != self.availability.organization_id
                or self.task.calendar_ref != self.availability.calendar_ref):
            raise ValueError("inference task/projection scope mismatch")
        if (self.availability.fetched_at > self.issued_at
                or self.issued_at >= self.availability.expires_at
                or self.deadline_at <= self.issued_at
                or self.deadline_at > self.issued_at + timedelta(seconds=120)
                or self.prompt_digest != sha256({
                    "instruction": PLANNER_INSTRUCTION,
                    "task": self.task, "availability": self.availability,
                })):
            raise ValueError("inference freshness or prompt binding failure")
        if self.task.simulation_ref != "simulation_pending":
            raise ValueError("unadmitted simulation reference cannot enter inference")
        return self


class InferenceReportedUsage(StrictModel):
    """Unverified backend-claimed usage. Never a trusted invoice or rate grant."""

    input_tokens: int = Field(ge=0, le=1_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    source: Literal["synthetic_mock", "provider_reported"]
    independently_verified: Literal[False] = False


class InferenceTransportReply(StrictModel):
    """Untrusted backend response; the gateway validates every bound field."""

    schema_version: Literal["safe_agent.inference_transport_reply.v1"] = (
        "safe_agent.inference_transport_reply.v1"
    )
    request_id: InferenceRequestId
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    output_json: str = Field(min_length=1, max_length=16_384)
    reported_usage: InferenceReportedUsage | None = None


class InferenceOutcome(StrictModel):
    """Sanitized, non-executable result of one typed inference request."""

    schema_version: Literal["safe_agent.inference_outcome.v1"] = (
        "safe_agent.inference_outcome.v1"
    )
    request_id: InferenceRequestId
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    proposal_digest: Digest
    proposal: Annotated[
        CalendarCreateEvent | RequestClarification,
        Field(discriminator="operation"),
    ]
    reported_usage: InferenceReportedUsage | None = None
    usage_independently_verified: Literal[False] = False
    model_authorized_execution: Literal[False] = False
    provider_effect_performed: Literal[False] = False
    synthetic_only: Literal[True] = True
