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
        if (
            record is None
            or record.revoked
            or record.organization_id != scope.organization_id
            or record.task_id != scope.task_id
            or record.initiator_id != scope.initiator_id
            or record.resource_kind != kind
            or scope.operation not in record.permitted_operations
            or scope.now >= record.expires_at
        ):
            raise ReferenceDenied("reference scope denied")
        return record.resource_id

    def revoke(self, handle: str) -> None:
        record = self._records.get(handle)
        if record is None:
            raise ReferenceDenied("reference scope denied")
        self._records[handle] = record.model_copy(update={"revoked": True})
