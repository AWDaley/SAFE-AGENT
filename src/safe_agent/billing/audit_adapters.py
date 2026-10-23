"""Aggregate provider-cost audit adapter boundary."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from safe_agent.contracts.provider_billing_audit import NormalizedProviderCostAudit


class ProviderCostAuditAdapter(ABC):
    """Verifier-side aggregate cost normalization; never request reconciliation."""

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def normalize_audit(
        self, payload: str, *, now: datetime,
    ) -> NormalizedProviderCostAudit:
        raise NotImplementedError
