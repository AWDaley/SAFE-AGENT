"""Versioned episode inference protocol; a model candidate carries no authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, SecretStr, TypeAdapter, field_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.episodes import CorrelationId, EpisodeDomain, OperationName


def wire_json(value: object) -> str:
    """Exact finite JSON bytes, without normalizing native benchmark identifiers."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def wire_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(wire_json(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def strict_json(value: str, *, max_bytes: int = 1_048_576) -> object:
    if not 0 < len(value.encode("utf-8")) <= max_bytes:
        raise ValueError("JSON byte limit exceeded")
    def invalid(_: str) -> None:
        raise ValueError("non-finite JSON number")
    def finite_number(raw: str) -> float:
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=invalid,
                      parse_float=finite_number)


class ToolCandidate(StrictModel):
    kind: Literal["tool"]
    tool_name: OperationName
    arguments: dict[str, JsonValue]


class MessageCandidate(StrictModel):
    kind: Literal["message"]
    purpose: Literal["question", "answer", "policy", "user_instruction", "success", "finish"]
    text: str = Field(min_length=1, max_length=8192)
    evidence_call_ids: list[CorrelationId] = Field(default_factory=list, max_length=16)
    stop: bool = False


EpisodeCandidate: TypeAlias = Annotated[ToolCandidate | MessageCandidate, Field(discriminator="kind")]
CANDIDATE_ADAPTER: TypeAdapter[EpisodeCandidate] = TypeAdapter(EpisodeCandidate)


def parse_candidate(raw: str) -> EpisodeCandidate:
    parsed = strict_json(raw, max_bytes=65_536)
    return CANDIDATE_ADAPTER.validate_python(parsed)


class EpisodeInferenceRequest(StrictModel):
    schema_version: Literal["safe_agent.episode_model_request.v1"] = (
        "safe_agent.episode_model_request.v1"
    )
    request_id: CorrelationId
    episode_id: Identifier
    domain: EpisodeDomain
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    policy_hash: Digest
    observation_hash: Digest
    projected_context: dict[str, JsonValue]
    max_output_tokens: int = Field(ge=64, le=16_384)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int | None = None
    issued_at: datetime
    deadline: datetime

    _aware = field_validator("issued_at", "deadline")(require_aware)

    @property
    def digest(self) -> str:
        return wire_digest(self.model_dump(mode="json"))


class EpisodeInferenceReply(StrictModel):
    schema_version: Literal["safe_agent.episode_model_reply.v1"] = (
        "safe_agent.episode_model_reply.v1"
    )
    request_id: CorrelationId
    input_hash: Digest
    model: str
    output_json: str = Field(min_length=1, max_length=65_536)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    # Provider usage is not an independent billing attestation.
    usage_source: Literal["scripted", "provider_reported"]

    @property
    def digest(self) -> str:
        return wire_digest(self.model_dump(mode="json"))


class EpisodeDispatchEnvelope(StrictModel):
    request: EpisodeInferenceRequest
    reservation_hash: Digest
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class EpisodeDispatchAuthenticator:
    """Separate host-owned dispatch key; no model field can mint a signature."""

    def __init__(self, key: SecretStr):
        raw = key.get_secret_value().encode("ascii")
        if not 32 <= len(raw) <= 512:
            raise ValueError("invalid episode dispatch key")
        self._key = raw

    def _signature(self, request: EpisodeInferenceRequest, reservation_hash: str) -> str:
        body = wire_json({"request_hash": request.digest, "reservation_hash": reservation_hash})
        return hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).hexdigest()

    def sign(self, request: EpisodeInferenceRequest, reservation_hash: str) -> EpisodeDispatchEnvelope:
        return EpisodeDispatchEnvelope(request=request, reservation_hash=reservation_hash,
                                       signature=self._signature(request, reservation_hash))

    def verify(self, envelope: EpisodeDispatchEnvelope) -> EpisodeInferenceRequest:
        if not hmac.compare_digest(envelope.signature,
                                   self._signature(envelope.request, envelope.reservation_hash)):
            raise ValueError("EPISODE_DISPATCH_AUTHENTICATION_FAILED")
        return envelope.request
