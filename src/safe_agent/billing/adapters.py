"""Closed billing-source adapter boundary.

Provider-specific readers belong here, inside the isolated verifier trust domain.
The current implementation intentionally exposes only the synthetic HMAC adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from safe_agent.billing.verifier import SyntheticBillingSourceRecord, _verify_source_record
from safe_agent.contracts.billing_verification import NormalizedBillingEvidence


class BillingEvidenceAdapter(ABC):
    """Trusted verifier-side normalization only; never an application tool."""

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, payload: str, *, now: datetime) -> NormalizedBillingEvidence:
        raise NotImplementedError


class SyntheticHMACBillingAdapter(BillingEvidenceAdapter):
    """Current offline fixture adapter; not a real provider invoice reader."""

    def __init__(self, *, source_key: bytes, max_evidence_age_seconds: int = 3600) -> None:
        if (type(source_key) is not bytes or len(source_key) < 32
                or not 1 <= max_evidence_age_seconds <= 86_400):
            raise ValueError("trusted synthetic billing source key required")
        self._source_key = source_key
        self._max_age = max_evidence_age_seconds

    @property
    def adapter_id(self) -> str:
        return "synthetic_hmac_v1"

    def normalize(self, payload: str, *, now: datetime) -> NormalizedBillingEvidence:
        try:
            record = SyntheticBillingSourceRecord.model_validate_json(payload)
            checked, source_hash = _verify_source_record(
                record, source_key=self._source_key, now=now,
                max_evidence_age_seconds=self._max_age,
            )
            return NormalizedBillingEvidence(
                source_adapter_id=self.adapter_id,
                source_record_id=checked.source_record_id,
                request_id=checked.request_id,
                organization_id=checked.organization_id,
                task_id=checked.task_id,
                route_id=checked.route_id,
                model_revision=checked.model_revision,
                prompt_digest=checked.prompt_digest,
                charge_microusd=checked.charge_microusd,
                source_evidence_hash=source_hash,
                source_observed_at=checked.issued_at,
            )
        except Exception as error:
            from safe_agent.billing.verifier import BillingSourceDenied

            if isinstance(error, BillingSourceDenied):
                raise
            raise BillingSourceDenied() from None
