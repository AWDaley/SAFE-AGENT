"""Closed contracts for durable, multi-turn benchmark episodes.

These are a new protocol.  They neither widen the office v1 contracts nor give a
model authority to execute tools.  ``AuthorizationBinding`` is a trusted-host
receipt produced *after* deterministic domain/TAL admission, never model output.
Private native arguments live in the host outbox; public evidence carries hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware

EpisodeDomain = Literal["retail", "banking_knowledge"]
OperationName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_\.]{0,127}$")
]
# Official benchmark call IDs are correlation values, not SAFE object IDs.
CorrelationId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
]
MAX_ARGUMENT_BYTES = 65536


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child)
        return
    raise ValueError("tool arguments must contain only finite JSON values")


def canonical_arguments_json(arguments: dict[str, Any]) -> str:
    """Exact native transport JSON, including finite benchmark numeric floats.

    The result is hashed as exact UTF-8 bytes.  TAL may bind this string together
    with its byte digest without admitting floats into TAL canonical data.
    """
    if type(arguments) is not dict:
        raise ValueError("tool arguments must be a JSON object")
    _json_value(arguments)
    result = json.dumps(
        arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(result.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("tool arguments exceed the transport bound")
    return result


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate tool argument key")
        result[key] = value
    return result


def validate_arguments_json(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("tool arguments exceed the transport bound")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_pairs)
        if canonical_arguments_json(parsed) != value:
            raise ValueError("tool argument JSON is not canonical")
    except (TypeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid tool argument JSON") from exc
    return value


def arguments_digest(arguments_json: str) -> str:
    """Digest exact UTF-8 bytes, including distinct Unicode representations."""
    value = validate_arguments_json(arguments_json)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def effect_digest(operation: str, arguments_hash: str) -> str:
    """Consent target independent of a freshly generated benchmark call ID."""
    return sha256({"operation": operation, "arguments_hash": arguments_hash})


class EpisodeLimits(StrictModel):
    max_turns: int = Field(ge=0, le=1_000_000_000)
    max_model_calls: int = Field(ge=0, le=1_000_000_000)
    max_tool_calls: int = Field(ge=0, le=1_000_000_000)
    max_tokens: int = Field(ge=0, le=1_000_000_000_000)
    max_cost_microusd: int = Field(ge=0, le=1_000_000_000_000_000)


class BudgetDelta(StrictModel):
    turns: int = Field(default=0, ge=0, le=1_000_000_000)
    model_calls: int = Field(default=0, ge=0, le=1_000_000_000)
    tool_calls: int = Field(default=0, ge=0, le=1_000_000_000)
    tokens: int = Field(default=0, ge=0, le=1_000_000_000_000)
    cost_microusd: int = Field(default=0, ge=0, le=1_000_000_000_000_000)


class RunSpec(StrictModel):
    schema_version: Literal["safe_agent.episode_run.v1"] = "safe_agent.episode_run.v1"
    run_id: Identifier
    organization_id: Identifier
    policy_hash: Digest
    limits: EpisodeLimits
    created_at: datetime
    deadline: datetime

    _aware = field_validator("created_at", "deadline")(require_aware)

    @model_validator(mode="after")
    def ordered(self) -> RunSpec:
        if self.deadline <= self.created_at:
            raise ValueError("run deadline must follow creation")
        return self


class EpisodeSpec(StrictModel):
    schema_version: Literal["safe_agent.episode_spec.v1"] = "safe_agent.episode_spec.v1"
    episode_id: Identifier
    run_id: Identifier
    actor_id: Identifier
    domain: EpisodeDomain
    policy_hash: Digest
    limits: EpisodeLimits
    created_at: datetime
    deadline: datetime

    _aware = field_validator("created_at", "deadline")(require_aware)

    @model_validator(mode="after")
    def ordered(self) -> EpisodeSpec:
        if self.deadline <= self.created_at:
            raise ValueError("episode deadline must follow creation")
        return self


class EpisodeState(StrEnum):
    READY = "READY"
    INFERENCE_PENDING = "INFERENCE_PENDING"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    TOOL_PENDING = "TOOL_PENDING"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    HANDOFF = "HANDOFF"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class EpisodeView(StrictModel):
    schema_version: Literal["safe_agent.episode_view.v1"] = "safe_agent.episode_view.v1"
    episode_id: Identifier
    run_id: Identifier
    actor_id: Identifier
    domain: EpisodeDomain
    policy_hash: Digest
    limits: EpisodeLimits
    state: EpisodeState
    state_version: int = Field(ge=0)
    ledger_sequence: int = Field(ge=1)
    usage: BudgetDelta = Field(default_factory=BudgetDelta)
    observation_hash: Digest
    pending_request_id: CorrelationId | None = None
    pending_call_id: CorrelationId | None = None
    last_event_hash: Digest
    created_at: datetime
    updated_at: datetime
    deadline: datetime

    _aware = field_validator("created_at", "updated_at", "deadline")(require_aware)


class RunView(StrictModel):
    spec: RunSpec
    usage: BudgetDelta
    state_version: int = Field(ge=0)


class InferenceReservation(StrictModel):
    schema_version: Literal["safe_agent.episode_inference.v1"] = "safe_agent.episode_inference.v1"
    episode_id: Identifier
    run_id: Identifier
    request_id: CorrelationId
    input_hash: Digest
    budget: BudgetDelta
    state_version: int = Field(ge=0)
    created_at: datetime
    result_hash: Digest | None = None
    uncertain: bool = False

    _aware = field_validator("created_at")(require_aware)


class OutgoingCall(StrictModel):
    schema_version: Literal["safe_agent.episode_call.v1"] = "safe_agent.episode_call.v1"
    call_id: CorrelationId
    operation: OperationName
    arguments_json: str = Field(min_length=2, max_length=MAX_ARGUMENT_BYTES)
    arguments_hash: Digest
    policy_hash: Digest
    observation_hash: Digest
    consent_hash: Digest | None = None

    _canonical_arguments = field_validator("arguments_json")(validate_arguments_json)

    @model_validator(mode="after")
    def argument_binding(self) -> OutgoingCall:
        if arguments_digest(self.arguments_json) != self.arguments_hash:
            raise ValueError("outgoing argument hash mismatch")
        return self

    @property
    def action_hash(self) -> str:
        return sha256(self)

    @property
    def effect_hash(self) -> str:
        return effect_digest(self.operation, self.arguments_hash)


class AuthorizationBinding(StrictModel):
    """Trusted host admission receipt; never include this in a model schema.

    The host must produce this only after operation allowlisting, TAL admission,
    current scoped references and any required consent/operator authorization.
    Merely parsing this record is not authorization.
    """

    episode_id: Identifier
    action_hash: Digest
    policy_hash: Digest
    observation_hash: Digest
    state_version: int = Field(ge=0)
    consent_hash: Digest | None = None
    domain_admission_hash: Digest | None = None
    allowed: Literal[True] = True


class PreparedCall(StrictModel):
    episode_id: Identifier
    call: OutgoingCall
    authorization: AuthorizationBinding
    prepared_at: datetime
    state_version: int = Field(ge=0)
    # This is a one-shot return value, never reconstructed as True from storage.
    emit_allowed: bool
    result_hash: Digest | None = None
    success: bool | None = None
    uncertain: bool = False

    _aware = field_validator("prepared_at")(require_aware)


class ToolResult(StrictModel):
    call_id: CorrelationId
    result_hash: Digest
    success: bool


class EpisodeEventKind(StrEnum):
    CREATED = "CREATED"
    USER_TURN = "USER_TURN"
    OBSERVATION = "OBSERVATION"
    INFERENCE_RESERVED = "INFERENCE_RESERVED"
    INFERENCE_FINISHED = "INFERENCE_FINISHED"
    INFERENCE_UNCERTAIN = "INFERENCE_UNCERTAIN"
    CALL_PREPARED = "CALL_PREPARED"
    CALL_RESULT = "CALL_RESULT"
    CALL_UNCERTAIN = "CALL_UNCERTAIN"
    RESPONSE = "RESPONSE"
    FINISHED = "FINISHED"


class EpisodeEvent(StrictModel):
    schema_version: Literal["safe_agent.episode_event.v1"] = "safe_agent.episode_event.v1"
    episode_id: Identifier
    event_id: Identifier
    kind: EpisodeEventKind
    sequence: int = Field(ge=1)
    state_version: int = Field(ge=0)
    occurred_at: datetime
    payload: dict[str, str]
    payload_hash: Digest
    previous_event_hash: Digest | None = None
    event_hash: Digest

    _aware = field_validator("occurred_at")(require_aware)
