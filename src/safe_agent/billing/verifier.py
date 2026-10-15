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
from safe_agent.contracts.inference import InferenceRequestId
from safe_agent.contracts.inference_budget import BillingChargeAttestation

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


class OfflineSyntheticBillingVerifier:
    """Separate source key and ledger attestation key; no remote/provider access."""

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
        try:
            checked = SyntheticBillingSourceRecord.model_validate_json(
                record.model_dump_json(),
            )
            source_payload = checked.model_dump(
                mode="json", exclude={"source_signature"},
            )
            expected = hmac.new(
                self._source_key, canonical_json(source_payload), hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, checked.source_signature):
                raise BillingSourceDenied()
            if (checked.issued_at > now
                    or now - checked.issued_at
                       > timedelta(seconds=self._max_age)):
                raise BillingSourceDenied()
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
                source_evidence_hash=sha256(source_payload),
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
        except BillingSourceDenied:
            raise
        except (ValueError, TypeError, AttributeError):
            raise BillingSourceDenied() from None


class IdentifierValidator:
    """Use the exact shared Identifier parser, never accept shell-style names."""

    @staticmethod
    def validate(value: str) -> str:
        from pydantic import TypeAdapter

        return TypeAdapter(Identifier).validate_python(value)
