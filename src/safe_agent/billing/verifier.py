"""Offline-only, separate synthetic billing evidence verification boundary.

This verifier accepts only source-signed SYNTHETIC statements and can issue
the M7-03 ledger's HMAC-bound BillingChargeAttestation. It cannot read model
usage, call a vendor's invoice API, or operate business provider tools.
Authenticity of the synthetic fixture is not proof of real provider billing.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.billing_verification import (
    NormalizedBillingEvidence,
    VerifiedBillingAttestation,
)
from safe_agent.contracts.inference import InferenceRequestId
from safe_agent.contracts.inference_budget import BillingChargeAttestation
from safe_agent.contracts.provider_billing_audit import (
    NormalizedProviderCostAudit,
    VerifiedProviderCostAudit,
)

SourceRecordId = Annotated[str, StringConstraints(pattern=r"^src_[0-9a-f]{32}$")]
HmacDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SyntheticBillingSourceRecord(StrictModel):
    """An isolated test source's charge, not model-reported tokens or invoice."""

    schema_version: Literal["safe_agent.synthetic_billing_source.v1"] = (
        "safe_agent.synthetic_billing_source.v1"
    )
    source_kind: Literal["synthetic_offline_only"] = "synthetic_offline_only"
    source_record_id: SourceRecordId
    request_id: InferenceRequestId
    organization_id: Identifier
    task_id: Identifier
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    charge_microusd: int = Field(ge=0, le=10_000_000)
    issued_at: datetime
    source_signature: HmacDigest

    @field_validator("issued_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class BillingSourceDenied(ValueError):
    """A fixed denial category with no raw invoice, key or source text."""

    def __init__(self) -> None:
        super().__init__("SYNTHETIC_BILLING_SOURCE_DENIED")


def _verify_source_record(
    record: SyntheticBillingSourceRecord, *, source_key: bytes,
    now: datetime, max_evidence_age_seconds: int,
) -> tuple[SyntheticBillingSourceRecord, str]:
    """Authenticate one source record and return its canonical evidence hash."""
    now = require_aware(now).astimezone(UTC)
    try:
        checked = SyntheticBillingSourceRecord.model_validate_json(record.model_dump_json())
        source_payload = checked.model_dump(mode="json", exclude={"source_signature"})
        expected = hmac.new(
            source_key, canonical_json(source_payload), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, checked.source_signature):
            raise BillingSourceDenied()
        if (checked.issued_at > now
                or now - checked.issued_at
                   > timedelta(seconds=max_evidence_age_seconds)):
            raise BillingSourceDenied()
        return checked, sha256(source_payload)
    except BillingSourceDenied:
        raise
    except (ValueError, TypeError, AttributeError):
        raise BillingSourceDenied() from None


class OfflineSyntheticBillingVerifier:
    """Legacy HMAC attestation fixture; retained for compatibility tests."""

    def __init__(
        self, *, source_key: bytes, issuer_key: bytes, issuer_id: str,
        max_evidence_age_seconds: int = 3600,
    ) -> None:
        if (type(source_key) is not bytes or type(issuer_key) is not bytes
                or len(source_key) < 32 or len(issuer_key) < 32
                or source_key == issuer_key
                or not 1 <= max_evidence_age_seconds <= 86_400):
            raise ValueError("separate trusted source and billing-verifier keys required")
        self._source_key = source_key
        self._issuer_key = issuer_key
        self._issuer = IdentifierValidator.validate(issuer_id)
        self._max_age = max_evidence_age_seconds

    def attest(
        self, record: SyntheticBillingSourceRecord, *, now: datetime,
    ) -> BillingChargeAttestation:
        now = require_aware(now).astimezone(UTC)
        checked, source_hash = _verify_source_record(
            record, source_key=self._source_key, now=now,
            max_evidence_age_seconds=self._max_age,
        )
        receipt_id = "bil_" + hmac.new(
            self._issuer_key,
            canonical_json({
                "source_record_id": checked.source_record_id,
                "request_id": checked.request_id,
            }),
            hashlib.sha256,
        ).hexdigest()[:32]
        unsigned = BillingChargeAttestation(
            receipt_id=receipt_id, request_id=checked.request_id,
            organization_id=checked.organization_id,
            task_id=checked.task_id, route_id=checked.route_id,
            model_revision=checked.model_revision,
            prompt_digest=checked.prompt_digest,
            charge_microusd=checked.charge_microusd,
            source_evidence_hash=source_hash,
            issuer_id=self._issuer, observed_at=now,
            signature="0" * 64,
        )
        digest = hmac.new(
            self._issuer_key,
            canonical_json(unsigned.model_dump(
                mode="json", exclude={"signature"},
            )),
            hashlib.sha256,
        ).hexdigest()
        return unsigned.model_copy(update={"signature": digest})


class OfflineAsymmetricBillingVerifier:
    """Sign normalized evidence; source/provider verification is adapter-owned."""

    def __init__(
        self, *, issuer_private_key: bytes,
        issuer_id: str, verifier_key_id: str,
    ) -> None:
        if type(issuer_private_key) is not bytes or len(issuer_private_key) != 32:
            raise ValueError("valid Ed25519 verifier key required")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            self._private = Ed25519PrivateKey.from_private_bytes(issuer_private_key)
        except Exception:
            raise ValueError("valid Ed25519 verifier key required") from None
        self._issuer = IdentifierValidator.validate(issuer_id)
        self._key_id = IdentifierValidator.validate(verifier_key_id)

    def sign(
        self, evidence: NormalizedBillingEvidence, *, now: datetime,
    ) -> VerifiedBillingAttestation:
        now = require_aware(now).astimezone(UTC)
        try:
            checked = NormalizedBillingEvidence.model_validate_json(evidence.model_dump_json())
            if checked.source_observed_at.astimezone(UTC) > now:
                raise BillingSourceDenied()
        except BillingSourceDenied:
            raise
        except Exception:
            raise BillingSourceDenied() from None
        receipt_id = "bil_" + sha256({
            "source_adapter_id": checked.source_adapter_id,
            "source_record_id": checked.source_record_id,
            "request_id": checked.request_id,
            "verifier_key_id": self._key_id,
        })[7:39]
        unsigned = VerifiedBillingAttestation(
            receipt_id=receipt_id, request_id=checked.request_id,
            organization_id=checked.organization_id,
            task_id=checked.task_id, route_id=checked.route_id,
            model_revision=checked.model_revision,
            prompt_digest=checked.prompt_digest,
            charge_microusd=checked.charge_microusd,
            source_evidence_hash=checked.source_evidence_hash,
            source_adapter_id=checked.source_adapter_id,
            issuer_id=self._issuer, verifier_key_id=self._key_id,
            observed_at=now, signature="0" * 128,
        )
        signature = self._private.sign(canonical_json(
            unsigned.model_dump(mode="json", exclude={"signature"}),
        )).hex()
        return unsigned.model_copy(update={"signature": signature})


    def sign_cost_audit(
        self, audit: NormalizedProviderCostAudit, *, now: datetime,
    ) -> VerifiedProviderCostAudit:
        """Sign aggregate provider costs without creating request settlement evidence."""
        current = require_aware(now).astimezone(UTC)
        try:
            checked = NormalizedProviderCostAudit.model_validate_json(audit.model_dump_json())
            if checked.source_observed_at.astimezone(UTC) > current:
                raise BillingSourceDenied()
        except BillingSourceDenied:
            raise
        except Exception:
            raise BillingSourceDenied() from None
        audit_id = "bca_" + sha256({
            "provider": checked.provider,
            "source_adapter_id": checked.source_adapter_id,
            "source_evidence_hash": checked.source_evidence_hash,
            "window_start": checked.window_start,
            "window_end": checked.window_end,
            "verifier_key_id": self._key_id,
        })[7:39]
        unsigned = VerifiedProviderCostAudit(
            audit_id=audit_id, audit=checked,
            issuer_id=self._issuer, verifier_key_id=self._key_id,
            observed_at=current, signature="0" * 128,
        )
        signature = self._private.sign(canonical_json(
            unsigned.model_dump(mode="json", exclude={"signature"}),
        )).hex()
        return unsigned.model_copy(update={"signature": signature})


class IdentifierValidator:
    """Use the exact shared Identifier parser, never accept shell-style names."""

    @staticmethod
    def validate(value: str) -> str:
        from pydantic import TypeAdapter

        return TypeAdapter(Identifier).validate_python(value)
