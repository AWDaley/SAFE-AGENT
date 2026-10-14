"""Sealed, hash-linked SAFE-AGENT office audit events and pure deterministic replay.

Office-domain events are *not* interoperable with TAL's invoice Event schema.
The ledger preserves the TAL event-sourcing invariants while calendar-domain
upstream conformance remains blocked (see docs/tal-compatibility.md).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from safe_agent.canonical import sha256
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
    ACTION_INTENT_RECORDED = "ACTION_INTENT_RECORDED"
    ACTION_VERIFIED = "ACTION_VERIFIED"
    ACTION_UNVERIFIED = "ACTION_UNVERIFIED"


class OfficePhase(StrEnum):
    ASSIGNED = "ASSIGNED"
    SIMULATED = "SIMULATED"
    PROPOSED_UNVERIFIED = "PROPOSED_UNVERIFIED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class TaskSnapshot(StrictModel):
    schema_version: Literal[
        "safe_agent.task_snapshot.v1", "safe_agent.task_snapshot.v2"
    ] = "safe_agent.task_snapshot.v1"
    task_id: Identifier
    organization_id: Identifier
    initiator_id: Identifier
    manifest_hash: Digest
    phase: OfficePhase = OfficePhase.ASSIGNED
    state_version: int = Field(ge=0)
    ledger_sequence: int = Field(ge=1)
    cycles_used: int = Field(default=0, ge=0)
    external_effects: int = Field(default=0, ge=0)
    effect_ref: Identifier | None = None
    verification_hash: Digest | None = None
    simulation_ref: Identifier | None = None
    proposal_hash: Digest | None = None
    approval_id: Identifier | None = None


class LedgerEvent(StrictModel):
    schema_version: Literal[
        "safe_agent.ledger_event.v1", "safe_agent.ledger_event.v2"
    ] = "safe_agent.ledger_event.v1"
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


def seal_event(*, task_id: str, organization_id: str, actor_id: str,
               kind: EventKind, sequence: int, state_version: int, now: datetime,
               payload: dict[str, str], previous_hash: str | None) -> LedgerEvent:
    """Compute a deterministic event digest from explicit trusted inputs."""
    m3_kinds = {EventKind.ACTION_INTENT_RECORDED,
                EventKind.ACTION_VERIFIED, EventKind.ACTION_UNVERIFIED}
    event_schema_version = (
        "safe_agent.ledger_event.v2" if kind in m3_kinds else "safe_agent.ledger_event.v1"
    )
    basis = {
        "schema_version": event_schema_version,
        "task_id": task_id,
        "organization_id": organization_id,
        "actor_id": actor_id,
        "kind": kind,
        "ledger_sequence": sequence,
        "state_version": state_version,
        "occurred_at": require_aware(now),
        "payload_digest": sha256(payload),
        "previous_event_hash": previous_hash,
    }
    digest = sha256(basis)
    return LedgerEvent(
        schema_version=event_schema_version,
        event_id="evt_" + digest[7:37],
        event_hash=digest,
        task_id=task_id,
        organization_id=organization_id,
        actor_id=actor_id,
        kind=kind,
        ledger_sequence=sequence,
        state_version=state_version,
        occurred_at=require_aware(now),
        payload_digest=sha256(payload),
        previous_event_hash=previous_hash,
        payload=payload,
    )


def _required(payload: dict[str, str], *fields: str) -> None:
    if set(payload) != set(fields) or not all(isinstance(x, str) and x for x in payload.values()):
        raise ValueError("invalid closed event payload")


def reduce_event(previous: TaskSnapshot | None, event: LedgerEvent) -> TaskSnapshot:
    """Pure projection; validates state transitions and enforces explicit schemas."""
    data = event.payload
    kind = event.kind
    if previous is None:
        if kind is not EventKind.TASK_CREATED or event.ledger_sequence != 1:
            raise ValueError("task ledger missing first genesis event")
        _required(data, "manifest_hash", "initiator_id")
        result = TaskSnapshot(
            task_id=event.task_id, organization_id=event.organization_id,
            initiator_id=data["initiator_id"], manifest_hash=data["manifest_hash"],
            ledger_sequence=1, state_version=0,
        )
    else:
        if (event.task_id != previous.task_id
                or event.organization_id != previous.organization_id
                or event.ledger_sequence != previous.ledger_sequence + 1):
            raise ValueError("out-of-order or out-of-scope ledger event")
        updates: dict[str, object] = {"ledger_sequence": event.ledger_sequence}
        if kind is EventKind.PREVIEW_REGISTERED:
            _required(data, "simulation_ref", "simulation_hash", "projection_rev")
            if previous.phase not in {
                OfficePhase.ASSIGNED, OfficePhase.SIMULATED, OfficePhase.READY_TO_COMMIT,
                OfficePhase.REJECTED, OfficePhase.AWAITING_APPROVAL,
            }:
                raise ValueError("preview registration not permitted in current phase")
            updates.update(phase=OfficePhase.SIMULATED,
                           simulation_ref=data["simulation_ref"],
                           proposal_hash=None, approval_id=None)
        elif kind is EventKind.PROPOSAL_ACCEPTED:
            _required(data, "proposal_hash", "simulation_ref")
            if (previous.phase is not OfficePhase.SIMULATED
                    or data["simulation_ref"] != previous.simulation_ref):
                raise ValueError("proposal must bind active simulated preview")
            updates.update(phase=OfficePhase.PROPOSED_UNVERIFIED,
                           proposal_hash=data["proposal_hash"],
                           cycles_used=previous.cycles_used + 1,
                           approval_id=None)
        elif kind is EventKind.PROPOSAL_DENIED:
            _required(data, "reason_code", "proposal_hash")
            # Audit-only: consumes a ledger sequence but does not change task state.
        elif kind is EventKind.APPROVAL_REQUIRED:
            _required(data, "proposal_hash")
            if (previous.phase is not OfficePhase.PROPOSED_UNVERIFIED
                    or data["proposal_hash"] != previous.proposal_hash):
                raise ValueError("approval request requires active proposal")
            updates["phase"] = OfficePhase.AWAITING_APPROVAL
        elif kind is EventKind.APPROVAL_GRANTED:
            _required(data, "approval_id", "proposal_hash")
            if (previous.phase is not OfficePhase.AWAITING_APPROVAL
                    or data["proposal_hash"] != previous.proposal_hash):
                raise ValueError("approval must match awaiting proposal")
            updates.update(phase=OfficePhase.READY_TO_COMMIT,
                           approval_id=data["approval_id"])
        elif kind is EventKind.APPROVAL_DENIED:
            _required(data, "approval_id", "proposal_hash")
            if (previous.phase is not OfficePhase.AWAITING_APPROVAL
                    or data["proposal_hash"] != previous.proposal_hash):
                raise ValueError("denial must match awaiting proposal")
            updates.update(phase=OfficePhase.REJECTED, approval_id=None)
        elif kind is EventKind.ACTION_ELIGIBLE:
            _required(data, "proposal_hash")
            if (previous.phase is not OfficePhase.PROPOSED_UNVERIFIED
                    or data["proposal_hash"] != previous.proposal_hash):
                raise ValueError("action eligibility requires active proposal")
            updates["phase"] = OfficePhase.READY_TO_COMMIT
        elif kind is EventKind.ACTION_INTENT_RECORDED:
            _required(data, "proposal_hash", "action_hash")
            if (previous.phase is not OfficePhase.READY_TO_COMMIT
                    or previous.proposal_hash != data["proposal_hash"]):
                raise ValueError("intent requires eligible active proposal")
            updates.update(phase=OfficePhase.EXECUTING,
                           schema_version="safe_agent.task_snapshot.v2")
        elif kind is EventKind.ACTION_VERIFIED:
            _required(data, "proposal_hash", "effect_ref", "verification_hash")
            if (previous.phase is not OfficePhase.EXECUTING
                    or previous.proposal_hash != data["proposal_hash"]
                    or previous.external_effects != 0):
                raise ValueError("verification requires one pending trusted effect")
            updates.update(
                phase=OfficePhase.COMPLETED, external_effects=previous.external_effects + 1,
                effect_ref=data["effect_ref"], verification_hash=data["verification_hash"],
            )
        elif kind is EventKind.ACTION_UNVERIFIED:
            _required(data, "proposal_hash", "reason_code")
            if (previous.phase is not OfficePhase.EXECUTING
                    or previous.proposal_hash != data["proposal_hash"]):
                raise ValueError("uncertain result requires pending intent")
            updates["phase"] = OfficePhase.NEEDS_REVIEW
        else:
            raise ValueError("unknown or duplicate task genesis event")
        mutates = kind is not EventKind.PROPOSAL_DENIED
        if mutates:
            updates["state_version"] = previous.state_version + 1
        result = previous.model_copy(update=updates)

    if event.state_version != result.state_version:
        raise ValueError("event projection version mismatch")
    return result


def replay(events: tuple[LedgerEvent, ...]) -> TaskSnapshot:
    """Verify sequence, every payload/hash link, and the replayable task projection."""
    if not events:
        raise ValueError("missing task genesis")
    state: TaskSnapshot | None = None
    preceding: str | None = None
    for event in events:
        if sha256(event.payload) != event.payload_digest:
            raise ValueError("event payload was tampered with")
        if event.previous_event_hash != preceding:
            raise ValueError("event predecessor hash mismatch")
        recomputed = seal_event(
            task_id=event.task_id, organization_id=event.organization_id,
            actor_id=event.actor_id, kind=event.kind,
            sequence=event.ledger_sequence, state_version=event.state_version,
            now=event.occurred_at, payload=event.payload, previous_hash=preceding,
        )
        if event.event_hash != recomputed.event_hash or event.event_id != recomputed.event_id:
            raise ValueError("event hash or identifier mismatch")
        state = reduce_event(state, event)
        preceding = event.event_hash
    assert state is not None
    return state
