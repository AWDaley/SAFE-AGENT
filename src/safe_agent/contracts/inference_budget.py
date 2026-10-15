"""M7-03 trusted, privacy-minimized inference spending and usage contracts.

All charges are estimates or unverified provider-reported quantities. No
ledger receipt is proof of provider billing, TAL authority or business effect.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from safe_agent.contracts.base import Digest, Identifier, StrictModel
from safe_agent.contracts.inference import InferenceRequestId

BudgetState = Literal["RESERVED", "RECORDED_UNVERIFIED", "UNCERTAIN"]
UncertaintyReason = Literal[
    "BACKEND_EXCEPTION", "LATE_RESPONSE", "INVALID_RESPONSE",
    "REPORTED_USAGE_OVERRUN", "EXPIRED_UNRECONCILED",
]


class InferenceBudgetLimits(StrictModel):
    """One trusted host profile; 0 cost cap prevents paid calls by default."""

    schema_version: Literal["safe_agent.inference_budget_limits.v1"] = (
        "safe_agent.inference_budget_limits.v1"
    )
    profile_id: Identifier
    global_max_microusd: int = Field(default=0, ge=0, le=1_000_000_000_000)
    tenant_max_microusd: int = Field(default=0, ge=0, le=1_000_000_000_000)
    task_max_microusd: int = Field(default=0, ge=0, le=1_000_000_000_000)
    global_max_calls: int = Field(default=100, ge=1, le=1_000_000)
    tenant_max_calls: int = Field(default=50, ge=1, le=1_000_000)
    task_max_calls: int = Field(default=3, ge=1, le=1_000_000)
    global_max_inflight: int = Field(default=10, ge=1, le=100)
    tenant_max_inflight: int = Field(default=5, ge=1, le=100)
    task_max_inflight: int = Field(default=1, ge=1, le=100)


class InferenceBudgetReceipt(StrictModel):
    schema_version: Literal["safe_agent.inference_budget_receipt.v1"] = (
        "safe_agent.inference_budget_receipt.v1"
    )
    request_id: InferenceRequestId
    organization_id: Identifier
    task_id: Identifier
    route_id: Identifier
    status: BudgetState
    maximum_held_microusd: int = Field(ge=0)
    reported_estimated_microusd: int | None = Field(default=None, ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    reported_source: Literal["synthetic_mock", "provider_reported"] | None = None
    uncertainty_reason: UncertaintyReason | None = None
    version: int = Field(ge=1)
    independently_verified: Literal[False] = False
    funds_released: Literal[False] = False
    model_authorized_execution: Literal[False] = False
    provider_effect_performed: Literal[False] = False


class InferenceBudgetScopeView(StrictModel):
    schema_version: Literal["safe_agent.inference_budget_scope.v1"] = (
        "safe_agent.inference_budget_scope.v1"
    )
    scope_key: str = Field(min_length=1, max_length=260)
    profile_hash: Digest
    max_microusd: int = Field(ge=0)
    held_microusd: int = Field(ge=0)
    max_calls: int = Field(ge=1)
    calls_used: int = Field(ge=0)
    max_inflight: int = Field(ge=1)
    active_calls: int = Field(ge=0)
    requires_review: bool = False
    verified_billing: Literal[False] = False
