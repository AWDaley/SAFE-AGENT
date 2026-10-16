"""Offline signed distribution for verifier public-key keyrings."""
from __future__ import annotations

from datetime import UTC, datetime

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.billing_distribution import (
    InstalledBillingKeyringState,
    PinnedBillingDistributionRoot,
    SignedBillingKeyringBundle,
)
from safe_agent.contracts.billing_verification import BillingVerifierKeyringConfig


class BillingKeyringDistributionDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("BILLING_KEYRING_DISTRIBUTION_DENIED")


def distribution_root(
    *, distribution_key_id: str, public_key: bytes, pinned_at: datetime,
) -> PinnedBillingDistributionRoot:
    if type(public_key) is not bytes or len(public_key) != 32:
        raise BillingKeyringDistributionDenied()
    try:
        return PinnedBillingDistributionRoot(
            distribution_key_id=distribution_key_id,
            public_key_hex=public_key.hex(),
            public_key_fingerprint=sha256({
                "algorithm": "ed25519",
                "distribution_key_id": distribution_key_id,
                "public_key_hex": public_key.hex(),
            }),
            pinned_at=require_aware(pinned_at).astimezone(UTC),
        )
    except Exception:
        raise BillingKeyringDistributionDenied() from None


class OfflineBillingKeyringDistributor:
    """Offline signer. Its private key is never an application/workspace secret."""

    def __init__(self, *, distribution_key_id: str, private_key: bytes) -> None:
        if type(private_key) is not bytes or len(private_key) != 32:
            raise BillingKeyringDistributionDenied()
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from pydantic import TypeAdapter

            from safe_agent.contracts.base import Identifier

            self._private = Ed25519PrivateKey.from_private_bytes(private_key)
            self._key_id = TypeAdapter(Identifier).validate_python(distribution_key_id)
        except Exception:
            raise BillingKeyringDistributionDenied() from None

    def sign(
        self, keyring: BillingVerifierKeyringConfig, *,
        generation: int, previous_bundle_digest: str | None,
        issued_at: datetime, expires_at: datetime,
    ) -> SignedBillingKeyringBundle:
        try:
            unsigned = SignedBillingKeyringBundle(
                distribution_key_id=self._key_id,
                generation=generation,
                previous_bundle_digest=previous_bundle_digest,
                issued_at=require_aware(issued_at).astimezone(UTC),
                expires_at=require_aware(expires_at).astimezone(UTC),
                keyring=BillingVerifierKeyringConfig.model_validate_json(
                    keyring.model_dump_json(),
                ),
                signature="0" * 128,
            )
            payload = canonical_json(unsigned.model_dump(
                mode="json", exclude={"signature"},
            ))
            return unsigned.model_copy(
                update={"signature": self._private.sign(payload).hex()},
            )
        except Exception:
            raise BillingKeyringDistributionDenied() from None


def _authenticate(
    bundle: SignedBillingKeyringBundle, *,
    root: PinnedBillingDistributionRoot, now: datetime,
) -> tuple[SignedBillingKeyringBundle, str]:
    current_time = require_aware(now).astimezone(UTC)
    checked = SignedBillingKeyringBundle.model_validate_json(bundle.model_dump_json())
    trusted = PinnedBillingDistributionRoot.model_validate_json(root.model_dump_json())
    if (checked.distribution_key_id != trusted.distribution_key_id
            or current_time < checked.issued_at.astimezone(UTC)
            or current_time >= checked.expires_at.astimezone(UTC)):
        raise BillingKeyringDistributionDenied()
    payload = checked.model_dump(mode="json", exclude={"signature"})
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted.public_key_hex)).verify(
        bytes.fromhex(checked.signature), canonical_json(payload),
    )
    return checked, sha256(payload)


def verify_bundle(
    bundle: SignedBillingKeyringBundle, *,
    root: PinnedBillingDistributionRoot,
    now: datetime,
    current: InstalledBillingKeyringState | None,
) -> tuple[SignedBillingKeyringBundle, str]:
    try:
        checked, digest = _authenticate(bundle, root=root, now=now)
        if current is None:
            if checked.generation != 1 or checked.previous_bundle_digest is not None:
                raise BillingKeyringDistributionDenied()
        else:
            current_checked = InstalledBillingKeyringState.model_validate_json(
                current.model_dump_json(),
            )
            if (checked.generation != current_checked.generation + 1
                    or checked.previous_bundle_digest != current_checked.bundle_digest
                    or checked.distribution_key_id != current_checked.distribution_key_id):
                raise BillingKeyringDistributionDenied()
        return checked, digest
    except BillingKeyringDistributionDenied:
        raise
    except Exception:
        raise BillingKeyringDistributionDenied() from None


def verify_installed_bundle(
    bundle: SignedBillingKeyringBundle, *,
    root: PinnedBillingDistributionRoot,
    state: InstalledBillingKeyringState,
    now: datetime,
) -> tuple[SignedBillingKeyringBundle, str]:
    try:
        checked, digest = _authenticate(bundle, root=root, now=now)
        installed = InstalledBillingKeyringState.model_validate_json(state.model_dump_json())
        if (checked.generation != installed.generation
                or checked.distribution_key_id != installed.distribution_key_id
                or digest != installed.bundle_digest):
            raise BillingKeyringDistributionDenied()
        return checked, digest
    except BillingKeyringDistributionDenied:
        raise
    except Exception:
        raise BillingKeyringDistributionDenied() from None
