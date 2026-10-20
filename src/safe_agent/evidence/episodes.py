"""Pure replay for the episode protocol; no provider, model or database access."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.episodes import (
    BudgetDelta,
    EpisodeEvent,
    EpisodeEventKind,
    EpisodeLimits,
    EpisodeSpec,
    EpisodeState,
    EpisodeView,
)


class EpisodeConflict(ValueError):
    """Closed-schema, stale state or mismatched correlation evidence."""


class EpisodeBudgetExceeded(EpisodeConflict):
    """A conservative run or episode allowance would be exceeded."""


def add_budget(usage: BudgetDelta, delta: BudgetDelta) -> BudgetDelta:
    return BudgetDelta.model_validate({
        name: getattr(usage, name) + getattr(delta, name)
        for name in BudgetDelta.model_fields
    })


def require_budget(usage: BudgetDelta, limits: EpisodeLimits) -> None:
    for name in BudgetDelta.model_fields:
        if getattr(usage, name) > getattr(limits, "max_" + name):
            raise EpisodeBudgetExceeded(f"episode budget exhausted: {name}")


def seal_episode_event(
    *, episode_id: str, kind: EpisodeEventKind, sequence: int, state_version: int,
    now: datetime, payload: dict[str, str], previous_hash: str | None,
) -> EpisodeEvent:
    basis = {
        "schema_version": "safe_agent.episode_event.v1",
        "episode_id": episode_id,
        "kind": kind,
        "sequence": sequence,
        "state_version": state_version,
        "occurred_at": require_aware(now),
        "payload_hash": sha256(payload),
        "previous_event_hash": previous_hash,
    }
    digest = sha256(basis)
    return EpisodeEvent.model_validate({
        **basis, "payload": payload, "event_id": "eevt_" + digest[7:39], "event_hash": digest,
    })


def _closed(payload: dict[str, str], *names: str) -> None:
    if set(payload) != set(names) or any(not value for value in payload.values()):
        raise EpisodeConflict("invalid closed episode event payload")


def _digest(value: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise EpisodeConflict("invalid event digest")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise EpisodeConflict("invalid event digest")


def _state(previous: EpisodeView, *states: EpisodeState) -> None:
    if previous.state not in states:
        raise EpisodeConflict(f"episode transition denied from {previous.state}")


def reduce_episode_event(previous: EpisodeView | None, event: EpisodeEvent) -> EpisodeView:
    """Validate event identity, linkage, closed payload and deterministic transition."""
    expected = seal_episode_event(
        episode_id=event.episode_id, kind=event.kind, sequence=event.sequence,
        state_version=event.state_version, now=event.occurred_at,
        payload=event.payload, previous_hash=event.previous_event_hash,
    )
    if expected != event:
        raise EpisodeConflict("episode event digest mismatch")
    data = event.payload
    kind = event.kind
    if previous is None:
        _closed(data, "spec_json")
        if (kind is not EpisodeEventKind.CREATED or event.sequence != 1
                or event.state_version != 0 or event.previous_event_hash is not None):
            raise EpisodeConflict("episode must begin with a genesis event")
        spec = EpisodeSpec.model_validate_json(data["spec_json"])
        if (canonical_json(spec).decode() != data["spec_json"]
                or spec.episode_id != event.episode_id or spec.created_at != event.occurred_at):
            raise EpisodeConflict("episode genesis identity mismatch")
        return EpisodeView(
            episode_id=spec.episode_id, run_id=spec.run_id, actor_id=spec.actor_id,
            domain=spec.domain, policy_hash=spec.policy_hash, limits=spec.limits,
            state=EpisodeState.READY, state_version=0, ledger_sequence=1,
            observation_hash=sha256({"episode_id": spec.episode_id, "initial": True}),
            last_event_hash=event.event_hash, created_at=spec.created_at,
            updated_at=spec.created_at, deadline=spec.deadline,
        )
    if (event.episode_id != previous.episode_id
            or event.sequence != previous.ledger_sequence + 1
            or event.state_version != previous.state_version + 1
            or event.previous_event_hash != previous.last_event_hash
            or event.occurred_at < previous.updated_at):
        raise EpisodeConflict("stale, reordered or cross-episode event")
    # Completion evidence must still be recorded after a deadline. New work may not.
    if (kind in {
        EpisodeEventKind.USER_TURN, EpisodeEventKind.OBSERVATION,
        EpisodeEventKind.INFERENCE_RESERVED, EpisodeEventKind.CALL_PREPARED,
        EpisodeEventKind.RESPONSE,
    } and event.occurred_at >= previous.deadline):
        raise EpisodeConflict("episode deadline elapsed")
    updates: dict[str, object] = {
        "state_version": event.state_version, "ledger_sequence": event.sequence,
        "last_event_hash": event.event_hash, "updated_at": event.occurred_at,
    }
    delta = BudgetDelta()
    if kind is EpisodeEventKind.USER_TURN:
        _closed(data, "message_id", "content_hash")
        _digest(data["content_hash"])
        _state(previous, EpisodeState.READY, EpisodeState.AWAITING_CUSTOMER)
        delta = BudgetDelta(turns=1)
        updates.update(state=EpisodeState.READY, observation_hash=sha256({
            "previous": previous.observation_hash, "kind": kind, **data,
        }))
    elif kind is EpisodeEventKind.OBSERVATION:
        _closed(data, "observation_hash")
        _digest(data["observation_hash"])
        _state(previous, EpisodeState.READY, EpisodeState.AWAITING_CUSTOMER)
        updates["observation_hash"] = data["observation_hash"]
    elif kind is EpisodeEventKind.INFERENCE_RESERVED:
        _closed(data, "request_id", "input_hash", "budget_json")
        _digest(data["input_hash"])
        _state(previous, EpisodeState.READY)
        delta = BudgetDelta.model_validate_json(data["budget_json"])
        if (delta.model_calls != 1 or delta.turns or delta.tool_calls
                or canonical_json(delta).decode() != data["budget_json"]):
            raise EpisodeConflict("inference reservation must count exactly one model call")
        updates.update(state=EpisodeState.INFERENCE_PENDING,
                       pending_request_id=data["request_id"])
    elif kind is EpisodeEventKind.INFERENCE_FINISHED:
        _closed(data, "request_id", "result_hash")
        _digest(data["result_hash"])
        _state(previous, EpisodeState.INFERENCE_PENDING)
        if previous.pending_request_id != data["request_id"]:
            raise EpisodeConflict("uncorrelated inference result")
        updates.update(state=EpisodeState.READY, pending_request_id=None)
    elif kind is EpisodeEventKind.INFERENCE_UNCERTAIN:
        _closed(data, "request_id", "reason_hash")
        _digest(data["reason_hash"])
        _state(previous, EpisodeState.INFERENCE_PENDING)
        if previous.pending_request_id != data["request_id"]:
            raise EpisodeConflict("uncorrelated uncertain inference")
        updates["state"] = EpisodeState.OUTCOME_UNCERTAIN
    elif kind is EpisodeEventKind.CALL_PREPARED:
        _closed(data, "call_id", "action_hash", "observation_hash", "authorization_hash")
        for name in ("action_hash", "observation_hash", "authorization_hash"):
            _digest(data[name])
        _state(previous, EpisodeState.READY)
        if data["observation_hash"] != previous.observation_hash:
            raise EpisodeConflict("call does not bind current observation")
        delta = BudgetDelta(tool_calls=1)
        updates.update(state=EpisodeState.TOOL_PENDING, pending_call_id=data["call_id"])
    elif kind is EpisodeEventKind.CALL_RESULT:
        _closed(data, "call_id", "result_hash", "success")
        _digest(data["result_hash"])
        _state(previous, EpisodeState.TOOL_PENDING, EpisodeState.OUTCOME_UNCERTAIN)
        if previous.pending_call_id != data["call_id"] or data["success"] not in {"true", "false"}:
            raise EpisodeConflict("uncorrelated tool result")
        updates.update(
            state=EpisodeState.READY, pending_call_id=None,
            observation_hash=sha256({"previous": previous.observation_hash, "kind": kind, **data}),
        )
    elif kind is EpisodeEventKind.CALL_UNCERTAIN:
        _closed(data, "call_id", "reason_hash")
        _digest(data["reason_hash"])
        _state(previous, EpisodeState.TOOL_PENDING)
        if previous.pending_call_id != data["call_id"]:
            raise EpisodeConflict("uncorrelated uncertain tool call")
        updates["state"] = EpisodeState.OUTCOME_UNCERTAIN
    elif kind is EpisodeEventKind.RESPONSE:
        _closed(data, "message_id", "content_hash", "awaiting_customer")
        _digest(data["content_hash"])
        _state(previous, EpisodeState.READY)
        if data["awaiting_customer"] not in {"true", "false"}:
            raise EpisodeConflict("invalid response disposition")
        if data["awaiting_customer"] == "true":
            updates["state"] = EpisodeState.AWAITING_CUSTOMER
    elif kind is EpisodeEventKind.FINISHED:
        _closed(data, "disposition")
        disposition = EpisodeState(data["disposition"])
        if disposition not in {EpisodeState.FINISHED, EpisodeState.HANDOFF, EpisodeState.FAILED}:
            raise EpisodeConflict("invalid terminal disposition")
        allowed = {EpisodeState.READY, EpisodeState.AWAITING_CUSTOMER}
        if disposition is EpisodeState.FAILED:
            allowed.add(EpisodeState.OUTCOME_UNCERTAIN)
        _state(previous, *allowed)
        # Uncertain pending identity is deliberately retained in a failed snapshot.
        updates["state"] = disposition
    else:
        raise EpisodeConflict("unsupported episode transition")
    usage = add_budget(previous.usage, delta)
    require_budget(usage, previous.limits)
    updates["usage"] = usage
    return EpisodeView.model_validate({**previous.model_dump(), **updates})


def replay_episode(events: Iterable[EpisodeEvent]) -> EpisodeView:
    snapshot: EpisodeView | None = None
    for event in events:
        snapshot = reduce_episode_event(snapshot, event)
    if snapshot is None:
        raise EpisodeConflict("empty episode journal")
    return snapshot


# Names parallel the existing office evidence module without altering its protocol.
seal_event = seal_episode_event
reduce_event = reduce_episode_event
replay = replay_episode
