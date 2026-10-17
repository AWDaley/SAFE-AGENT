"""Provider aggregate-cost audit contracts.

Aggregate provider cost data can audit spend, but it cannot settle one inference
request unless the provider source contains a one-to-one request charge.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.billing_verification import Ed25519Signature

DecimalUSD = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,12})?$"),
]
ProviderLabel = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class NormalizedProviderCostAudit(StrictModel):
    """Closed aggregate spend observation; explicitly not request reconciliation."""

    schema_version: Literal["safe_agent.provider_cost_audit.v1"] = (
        "safe_agent.provider_cost_audit.v1"
    )
    provider: Literal["openai"]
    source_adapter_id: Identifier
    source_evidence_hash: Digest
    source_observed_at: datetime
    window_start: datetime
    window_end: datetime
    currency: Literal["usd"] = "usd"
    total_usd_decimal: DecimalUSD
    bucket_count: int = Field(ge=1, le=180)
    result_count: int = Field(ge=0, le=100_000)
    project_ids: tuple[ProviderLabel, ...] = Field(max_length=256)
    line_items: tuple[ProviderLabel, ...] = Field(max_length=256)
    request_reconciliation_supported: Literal[False] = False
    request_reconciliation_block_reason: Literal[
        "PROVIDER_COSTS_ARE_AGGREGATED"
    ] = "PROVIDER_COSTS_ARE_AGGREGATED"

    @field_validator("source_observed_at", "window_start", "window_end")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("window_end")
    @classmethod
    def ordered_window(cls, value: datetime, info):
        start = info.data.get("window_start")
        if start is not None and value <= start:
            raise ValueError("PROVIDER_COST_WINDOW_DENIED")
        return value


class VerifiedProviderCostAudit(StrictModel):
    """Verifier-signed aggregate provider-cost evidence."""

    schema_version: Literal["safe_agent.verified_provider_cost_audit.v1"] = (
        "safe_agent.verified_provider_cost_audit.v1"
    )
    audit_id: Annotated[str, StringConstraints(pattern=r"^bca_[0-9a-f]{32}$")]
    audit: NormalizedProviderCostAudit
    issuer_id: Identifier
    verifier_key_id: Identifier
    observed_at: datetime
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: Ed25519Signature

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
