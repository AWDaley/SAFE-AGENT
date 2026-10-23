"""Opaque, non-bearer reference handles with privileged resource resolution.

This broker is a *scoping* control, not a replacement for the TAL action gate.
The agent sees handle strings only. Real IDs and mappings remain broker-side.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from safe_agent.canonical import canonical_json
from safe_agent.contracts.base import Identifier, StrictModel, require_aware

Operation = Literal["calendar.create_event", "tasks.create"]


class ResourceKind(StrEnum):
    CALENDAR = "calendar"
    CUSTOMER = "customer"
    INTERNAL_TASK = "internal_task"


class ReferenceDenied(ValueError):
    """No distinction between unknown and unauthorized references is exposed."""


class BrokerScope(StrictModel):
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    operation: Operation
    now: datetime

    @field_validator("now")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class _ReferenceRecord(StrictModel):
    handle: Identifier
    binding_id: Identifier
    resource_id: Identifier  # Trusted provider/fixture identifier; NEVER model-visible.
    resource_kind: ResourceKind
    organization_id: Identifier
    initiator_id: Identifier
    task_id: Identifier
    permitted_operations: tuple[Operation, ...] = Field(min_length=1)
    expires_at: datetime
    revoked: bool = False

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class ReferenceBroker:
    """In-memory trusted reference map; persistence and key rotation are M2+ work.

    The optional secret is exclusively for reproducible synthetic tests. Production
    deployments must supply a strongly random secret and persist protected mappings.
    Possessing a handle never grants access: every resolution also checks the scope.
    """

    def __init__(self, *, secret: bytes | None = None) -> None:
        self._key = secret if secret is not None else secrets.token_bytes(32)
        if len(self._key) < 32:
            raise ValueError("broker key requires at least 32 bytes")
        self._records: dict[str, _ReferenceRecord] = {}
        self._issued: dict[bytes, str] = {}
        self._serial = 0

    def mint(
        self,
        *,
        resource_id: str,
        resource_kind: ResourceKind,
        scope: BrokerScope,
        permitted_operations: tuple[Operation, ...],
        expires_at: datetime,
    ) -> str:
        require_aware(expires_at)
        if expires_at <= scope.now:
            raise ReferenceDenied("reference scope denied")
        if not permitted_operations or scope.operation not in permitted_operations:
            raise ReferenceDenied("reference scope denied")
        if len(permitted_operations) != len(set(permitted_operations)):
            raise ReferenceDenied("reference scope denied")
        # Idempotent mint for the same trusted request; avoids unstable
        # task-list projections when an identical observation is repeated.
        identity = canonical_json({"kind": resource_kind, "resource_id": resource_id,
                                   "scope": scope, "operations": permitted_operations,
                                   "expiry": expires_at})
        old_handle = self._issued.get(identity)
        if old_handle is not None and not self._records[old_handle].revoked:
            return old_handle
        self._serial += 1
        payload = canonical_json({"serial": self._serial, "identity": identity.hex()})
        digest = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        handle = "ref_" + digest[:40]
        record = _ReferenceRecord(
            handle=handle,
            binding_id="binding_" + digest[40:],
            resource_id=resource_id,
            resource_kind=resource_kind,
            organization_id=scope.organization_id,
            initiator_id=scope.initiator_id,
            task_id=scope.task_id,
            permitted_operations=permitted_operations,
            expires_at=expires_at,
        )
        self._records[handle] = record
        self._issued[identity] = handle
        return handle

    def resolve(self, handle: str, *, scope: BrokerScope, kind: ResourceKind) -> str:
        """Trusted operation: returns a protected resource ID, never to the model."""
        record = self._records.get(handle)
        return self._resolve_record(record, scope=scope, kind=kind, readback=False)

    @staticmethod
    def _resolve_record(record: _ReferenceRecord | None, *, scope: BrokerScope,
                        kind: ResourceKind, readback: bool) -> str:
        if (
            record is None
            or (record.revoked and not readback)
            or record.organization_id != scope.organization_id
            or record.task_id != scope.task_id
            or record.initiator_id != scope.initiator_id
            or record.resource_kind != kind
            or scope.operation not in record.permitted_operations
            or scope.now >= record.expires_at
        ):
            raise ReferenceDenied("reference scope denied")
        return record.resource_id

    def trusted_resolve_readback(
        self, handle: str, *, protected_records: tuple[dict[str, object], ...],
        scope: BrokerScope, kind: ResourceKind,
    ) -> str:
        """Resolve an existing action's identity for READBACK ONLY after revocation.

        The worker supplies its durable intent's original scope/time. A verified
        revoked mapping can identify an already committed effect, but this method
        never imports or reactivates it in the broker used for new dispatch.
        """
        matches = [entry for entry in protected_records if entry.get("handle") == handle]
        if len(matches) != 1:
            raise ReferenceDenied("reference scope denied")
        return self._resolve_record(
            self._verified_record(matches[0]), scope=scope, kind=kind, readback=True,
        )

    def revoke(self, handle: str) -> None:
        record = self._records.get(handle)
        if record is None:
            raise ReferenceDenied("reference scope denied")
        self._records[handle] = record.model_copy(update={"revoked": True})

    def trusted_export(self, *, task_id: str) -> tuple[dict[str, object], ...]:
        """Export signed *private* bindings exclusively to protected persistence.

        NEVER serialize this return value in an agent prompt, HTTP response or
        evidence payload. HMAC detects accidental or malicious DB-row tampering.
        """
        records: list[dict[str, object]] = []
        for handle, record in sorted(self._records.items()):
            if record.task_id != task_id:
                continue
            protected = record.model_dump(mode="json")
            signature = hmac.new(self._key, canonical_json(protected), hashlib.sha256).hexdigest()
            records.append({"handle": handle, "record": protected, "mac": signature})
        return tuple(records)

    def trusted_import(self, protected_records: tuple[dict[str, object], ...]) -> None:
        """Verify integrity before restoring privileged mappings after a restart."""
        for entry in protected_records:
            record = self._verified_record(entry)
            existing = self._records.get(record.handle)
            if existing is not None and existing != record:
                raise ReferenceDenied("reference scope denied")
            self._records[record.handle] = record
        self._serial = max(self._serial, len(self._records))

    def _verified_record(self, entry: dict[str, object]) -> _ReferenceRecord:
        if set(entry) != {"handle", "record", "mac"}:
            raise ReferenceDenied("reference scope denied")
        payload, mac, handle = entry["record"], entry["mac"], entry["handle"]
        if not isinstance(payload, dict) or not isinstance(mac, str) or not isinstance(handle, str):
            raise ReferenceDenied("reference scope denied")
        expected = hmac.new(self._key, canonical_json(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, mac):
            raise ReferenceDenied("reference scope denied")
        record = _ReferenceRecord.model_validate_json(canonical_json(payload).decode("utf-8"))
        if record.handle != handle:
            raise ReferenceDenied("reference scope denied")
        return record
