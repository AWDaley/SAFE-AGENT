"""M7-03 immutable, privacy-minimized inference cost and billing contracts.

All integer costs use MICRO-USD (1,000,000 units = $1). Provider-reported
tokens are unverified telemetry, not charge evidence. None of these models
grants execution authority or admits a task proposal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequestId

ReceiptId = Annotated[str, StringConstraints(pattern=r"^bil_[0-9a-f]{32}$")]
Signature = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class InferenceBudgetLimits(StrictModel):
    """Immutable account ceilings provided only at trusted service bootstrap."""

    schema_version: Literal["safe_agent.inference_budget_limits.v1"] = (
        "safe_agent.inference_budget_limits.v1"
    )
    global_microusd: int = Field(ge=0, le=10_000_000_000)
    tenant_microusd: int = Field(ge=0, le=10_000_000_000)
    task_microusd: int = Field(ge=0, le=10_000_000_000)
    max_call_microusd: int = Field(ge=0, le=10_000_000)


class InferenceReservationReceipt(StrictModel):
    schema_version: Literal["safe_agent.inference_reservation.v1"] = (
        "safe_agent.inference_reservation.v1"
    )
    request_id: InferenceRequestId
    task_id: Identifier
    status: Literal["IN_FLIGHT", "UNCERTAIN", "RECONCILED", "SYNTHETIC_SETTLED"]
    held_microusd: int = Field(ge=0)
    charged_microusd: int = Field(ge=0)
    max_estimated_microusd: int = Field(ge=0, le=10_000_000)
    evidence_hash: Digest | None = None
    ledger_hash: Digest
    usage_independently_verified: Literal[False] = False
    effect_authorized: Literal[False] = False


class InferenceAccountBalance(StrictModel):
    """Tenant-authorized, read-only account view. No cross-tenant disclosure."""

    scope: Literal["GLOBAL", "TENANT", "TASK"]
    limit_microusd: int = Field(ge=0)
    held_microusd: int = Field(ge=0)
    charged_microusd: int = Field(ge=0)
    available_microusd: int = Field(ge=0)
    version: int = Field(ge=0)

    @model_validator(mode="after")
    def conserved(self) -> InferenceAccountBalance:
        if (self.charged_microusd + self.held_microusd
                + self.available_microusd != self.limit_microusd):
            raise ValueError("inference budget account does not conserve capacity")
        return self


class InferenceBudgetView(StrictModel):
    schema_version: Literal["safe_agent.inference_budget_view.v1"] = (
        "safe_agent.inference_budget_view.v1"
    )
    task_id: Identifier
    tenant: InferenceAccountBalance
    task: InferenceAccountBalance
    global_remaining_microusd: int = Field(ge=0)
    billing_charge_verified: Literal[False] = False
    live_provider_effect_performed: Literal[False] = False


class BillingChargeAttestation(StrictModel):
    """Independently issued, HMAC-sealed assertion over externally verified costs.

    HMAC authenticates *who asserted* the charge; it does NOT prove that
    an external provider actually charged it. The signer must live in a
    separately reviewed billing verification service, not the model/gateway.
    """

    schema_version: Literal["safe_agent.billing_charge_attestation.v1"] = (
        "safe_agent.billing_charge_attestation.v1"
    )
    receipt_id: ReceiptId
    request_id: InferenceRequestId
    organization_id: Identifier
    task_id: Identifier
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    charge_microusd: int = Field(ge=0, le=10_000_000)
    source_evidence_hash: Digest
    issuer_id: Identifier
    observed_at: datetime
    signature: Signature

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
