"""Billing verifier signatures, key rotation and normalized source evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequestId
from safe_agent.contracts.inference_budget import ReceiptId

Ed25519Signature = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
Ed25519PublicKeyHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class NormalizedBillingEvidence(StrictModel):
    """Provider/source-adapter output before verifier signing."""

    schema_version: Literal["safe_agent.normalized_billing_evidence.v1"] = (
        "safe_agent.normalized_billing_evidence.v1"
    )
    source_adapter_id: Identifier
    source_record_id: Identifier
    request_id: InferenceRequestId
    organization_id: Identifier
    task_id: Identifier
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    charge_microusd: int = Field(ge=0, le=10_000_000)
    source_evidence_hash: Digest
    source_observed_at: datetime

    @field_validator("source_observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


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
    source_adapter_id: Identifier
    issuer_id: Identifier
    verifier_key_id: Identifier
    observed_at: datetime
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: Ed25519Signature

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class BillingVerifierPublicKey(StrictModel):
    """One immutable verifier public-key epoch with bounded late acceptance."""

    schema_version: Literal["safe_agent.billing_verifier_public_key.v1"] = (
        "safe_agent.billing_verifier_public_key.v1"
    )
    issuer_id: Identifier
    verifier_key_id: Identifier
    public_key_hex: Ed25519PublicKeyHex
    signing_not_before: datetime
    signing_not_after: datetime
    accept_until: datetime

    @field_validator("signing_not_before", "signing_not_after", "accept_until")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bounded_window(self) -> BillingVerifierPublicKey:
        if not self.signing_not_before < self.signing_not_after <= self.accept_until:
            raise ValueError("BILLING_KEY_WINDOW_DENIED")
        if self.signing_not_after - self.signing_not_before > timedelta(days=370):
            raise ValueError("BILLING_KEY_WINDOW_DENIED")
        if self.accept_until - self.signing_not_after > timedelta(days=30):
            raise ValueError("BILLING_KEY_GRACE_DENIED")
        return self


class BillingVerifierKeyringConfig(StrictModel):
    """Trusted reconciler configuration; never model or operator payload."""

    schema_version: Literal["safe_agent.billing_verifier_keyring.v1"] = (
        "safe_agent.billing_verifier_keyring.v1"
    )
    issuer_id: Identifier
    keys: tuple[BillingVerifierPublicKey, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_and_bound(self) -> BillingVerifierKeyringConfig:
        ids: set[str] = set()
        public_keys: set[str] = set()
        for key in self.keys:
            if (key.issuer_id != self.issuer_id
                    or key.verifier_key_id in ids
                    or key.public_key_hex in public_keys):
                raise ValueError("BILLING_KEYRING_DENIED")
            ids.add(key.verifier_key_id)
            public_keys.add(key.public_key_hex)
        return self
