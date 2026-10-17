"""Trusted Ed25519 verifier-key rotation for billing reconciliation."""
from __future__ import annotations

from datetime import UTC, datetime

from safe_agent.canonical import sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.billing_verification import (
    BillingVerifierKeyringConfig,
    BillingVerifierPublicKey,
    VerifiedBillingAttestation,
)


class BillingVerifierKeyDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("BILLING_VERIFIER_KEY_DENIED")


class TrustedBillingVerifierKeyring:
    """Closed public-key set with explicit signing and late-acceptance windows."""

    def __init__(self, config: BillingVerifierKeyringConfig) -> None:
        if type(config) is not BillingVerifierKeyringConfig:
            raise TypeError("trusted billing verifier keyring config required")
        checked = BillingVerifierKeyringConfig.model_validate_json(config.model_dump_json())
        self._config = checked
        self._keys = {item.verifier_key_id: item for item in checked.keys}

    @property
    def issuer_id(self) -> str:
        return self._config.issuer_id

    def resolve(
        self, evidence: VerifiedBillingAttestation, *, now: datetime,
    ) -> tuple[bytes, str]:
        """Return exact verification key + fingerprint or fail closed."""
        current = require_aware(now).astimezone(UTC)
        try:
            checked = VerifiedBillingAttestation.model_validate_json(evidence.model_dump_json())
            key: BillingVerifierPublicKey | None = self._keys.get(checked.verifier_key_id)
            if (key is None or checked.issuer_id != self._config.issuer_id):
                raise BillingVerifierKeyDenied()
            observed = checked.observed_at.astimezone(UTC)
            if (not key.signing_not_before.astimezone(UTC) <= observed
                    < key.signing_not_after.astimezone(UTC)
                    or current < observed
                    or current > key.accept_until.astimezone(UTC)):
                raise BillingVerifierKeyDenied()
            raw = bytes.fromhex(key.public_key_hex)
            if len(raw) != 32:
                raise BillingVerifierKeyDenied()
            fingerprint = sha256({
                "algorithm": "ed25519",
                "issuer_id": key.issuer_id,
                "verifier_key_id": key.verifier_key_id,
                "public_key_hex": key.public_key_hex,
            })
            return raw, fingerprint
        except BillingVerifierKeyDenied:
            raise
        except Exception:
            raise BillingVerifierKeyDenied() from None
