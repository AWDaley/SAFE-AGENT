"""Asymmetric billing verification contracts.

These records prove only that the configured billing verifier signed a bounded
billing assertion. They do not authorize business effects or prove that a real
provider invoice exists.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequestId
from safe_agent.contracts.inference_budget import ReceiptId

Ed25519Signature = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]


class VerifiedBillingAttestation(StrictModel):
    """Verifier-signed billing evidence; never an execution or retry permit."""

    schema_version: Literal["safe_agent.verified_billing_attestation.v1"] = (
        "safe_agent.verified_billing_attestation.v1"
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
    verifier_key_id: Identifier
    observed_at: datetime
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: Ed25519Signature

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
