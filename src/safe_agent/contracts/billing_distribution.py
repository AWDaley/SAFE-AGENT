"""Signed distribution contracts for billing-verifier keyrings."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.billing_verification import (
    BillingVerifierKeyringConfig,
    Ed25519PublicKeyHex,
    Ed25519Signature,
)


class PinnedBillingDistributionRoot(StrictModel):
    schema_version: Literal["safe_agent.billing_distribution_root.v1"] = (
        "safe_agent.billing_distribution_root.v1"
    )
    distribution_key_id: Identifier
    public_key_hex: Ed25519PublicKeyHex
    public_key_fingerprint: Digest
    pinned_at: datetime

    @field_validator("pinned_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class SignedBillingKeyringBundle(StrictModel):
    schema_version: Literal["safe_agent.signed_billing_keyring.v1"] = (
        "safe_agent.signed_billing_keyring.v1"
    )
    distribution_key_id: Identifier
    generation: int = Field(ge=1, le=2_147_483_647)
    previous_bundle_digest: Digest | None = None
    issued_at: datetime
    expires_at: datetime
    keyring: BillingVerifierKeyringConfig
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: Ed25519Signature

    @field_validator("issued_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bounded_lifetime(self) -> SignedBillingKeyringBundle:
        if not self.issued_at < self.expires_at:
            raise ValueError("BILLING_KEYRING_BUNDLE_WINDOW_DENIED")
        if self.expires_at - self.issued_at > timedelta(days=90):
            raise ValueError("BILLING_KEYRING_BUNDLE_WINDOW_DENIED")
        if self.generation == 1 and self.previous_bundle_digest is not None:
            raise ValueError("BILLING_KEYRING_CHAIN_DENIED")
        if self.generation > 1 and self.previous_bundle_digest is None:
            raise ValueError("BILLING_KEYRING_CHAIN_DENIED")
        return self


class InstalledBillingKeyringState(StrictModel):
    schema_version: Literal["safe_agent.installed_billing_keyring.v1"] = (
        "safe_agent.installed_billing_keyring.v1"
    )
    distribution_key_id: Identifier
    generation: int = Field(ge=1, le=2_147_483_647)
    bundle_digest: Digest
    installed_at: datetime

    @field_validator("installed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
