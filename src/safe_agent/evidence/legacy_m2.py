"""Frozen M2 wire-schema reference classes, not executable current state machines.

Do not use these for M3 runtime replay; they only preserve the exact version-1
schema export even when later event kinds/phase values are introduced.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware


class EventKind(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    PREVIEW_REGISTERED = "PREVIEW_REGISTERED"
    PROPOSAL_ACCEPTED = "PROPOSAL_ACCEPTED"
    PROPOSAL_DENIED = "PROPOSAL_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    ACTION_ELIGIBLE = "ACTION_ELIGIBLE"


class OfficePhase(StrEnum):
    ASSIGNED = "ASSIGNED"
    SIMULATED = "SIMULATED"
    PROPOSED_UNVERIFIED = "PROPOSED_UNVERIFIED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    REJECTED = "REJECTED"


class TaskSnapshot(StrictModel):
    schema_version: Literal["safe_agent.task_snapshot.v1"] = "safe_agent.task_snapshot.v1"
    task_id: Identifier
    organization_id: Identifier
    initiator_id: Identifier
    manifest_hash: Digest
    phase: OfficePhase = OfficePhase.ASSIGNED
    state_version: int = Field(ge=0)
    ledger_sequence: int = Field(ge=1)
    cycles_used: int = Field(default=0, ge=0)
    external_effects: Literal[0] = 0
    simulation_ref: Identifier | None = None
    proposal_hash: Digest | None = None
    approval_id: Identifier | None = None


class LedgerEvent(StrictModel):
    schema_version: Literal["safe_agent.ledger_event.v1"] = "safe_agent.ledger_event.v1"
    event_id: Identifier
    task_id: Identifier
    organization_id: Identifier
    actor_id: Identifier
    kind: EventKind
    ledger_sequence: int = Field(ge=1)
    state_version: int = Field(ge=0)
    occurred_at: datetime
    payload: dict[str, str]
    payload_digest: Digest
    previous_event_hash: Digest | None = None
    event_hash: Digest

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class Principal(StrictModel):
    organization_id: Identifier
    actor_id: Identifier
    scopes: tuple[Literal[
        "task:create", "task:prepare", "task:propose", "task:approve", "task:read", "task:audit"
    ], ...]
    authenticated_until: datetime

    @field_validator("authenticated_until")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)
