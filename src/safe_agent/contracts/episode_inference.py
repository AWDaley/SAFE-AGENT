"""Versioned episode inference protocol; a model candidate carries no authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, SecretStr, TypeAdapter, field_validator, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.episodes import CorrelationId, EpisodeDomain, OperationName


def validate_episode_model_arguments(
    arguments: Mapping[str, object], *, max_output_tokens: int
) -> None:
    """Accept explicit matched controls without changing SAFE's hard limits.

    The stock harness names its output cap ``max_tokens``. SAFE's authoritative
    cap remains the runtime budget; an alias may only restate that exact value.
    Explicit provider retries may only restate SAFE's zero-retry policy.
    Temperature and seed are validated by the inference request contract.
    """
    if set(arguments) - {"temperature", "seed", "num_retries", "max_tokens"}:
        raise ValueError("UNSUPPORTED_EPISODE_MODEL_ARGUMENTS")
    if "num_retries" in arguments:
        retries = arguments["num_retries"]
        if type(retries) is not int or retries != 0:
            raise ValueError("EPISODE_PROVIDER_RETRIES_MUST_BE_ZERO")
    if "max_tokens" in arguments:
        tokens = arguments["max_tokens"]
        if type(tokens) is not int or tokens != max_output_tokens:
            raise ValueError("EPISODE_OUTPUT_CAP_MUST_MATCH_RUNTIME_LIMIT")


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


def validate_local_api_base(value: str) -> str:
    """One canonical numeric loopback origin; no DNS or URL reinterpretation."""
    match = (re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1", value)
             if isinstance(value, str) else None)
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise ValueError("LOCAL_MODEL_API_BASE_DENIED")
    return value


class EpisodeBackendRoute(StrictModel):
    """Explicit service selection, separate from ambient provider settings."""

    kind: Literal["hosted_openai", "local_openai"] = "hosted_openai"
    local_api_base: str | None = None

    @model_validator(mode="after")
    def coherent_route(self) -> "EpisodeBackendRoute":
        if self.kind == "local_openai":
            if self.local_api_base is None:
                raise ValueError("LOCAL_MODEL_API_BASE_REQUIRED")
            validate_local_api_base(self.local_api_base)
        elif self.local_api_base is not None:
            raise ValueError("HOSTED_MODEL_LOCAL_API_BASE_DENIED")
        return self


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


class EpisodeBackendCapabilities(StrictModel):
    """Public, immutable configuration; never provider access or quota proof."""

    profile: Literal["openai_chat_v1", "local_openai_chat_v1", "scripted_fixture_v1"]
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    provider_origin: str | None
    timeout_seconds: float = Field(gt=0.0, le=120.0)
    provider_retries: Literal[0] = 0
    follow_redirects: Literal[False] = False
    trust_env: Literal[False] = False
    response_format: Literal["json_object", "scripted_fixture"]
    candidate_schema_hash: Digest
    system_instruction_hash: Digest

    @model_validator(mode="after")
    def coherent_profile(self) -> "EpisodeBackendCapabilities":
        if self.profile == "openai_chat_v1":
            if (self.provider_origin != "https://api.openai.com/v1"
                    or self.response_format != "json_object"):
                raise ValueError("invalid hosted capability profile")
        elif self.profile == "local_openai_chat_v1":
            if self.provider_origin is None or self.response_format != "json_object":
                raise ValueError("invalid local capability profile")
            validate_local_api_base(self.provider_origin)
        elif self.provider_origin is not None or self.response_format != "scripted_fixture":
            raise ValueError("fixture capabilities must be explicitly labelled")
        return self


class EpisodeServiceCapabilities(StrictModel):
    schema_version: Literal["safe_agent.episode_capabilities.v1"] = "safe_agent.episode_capabilities.v1"
    backend: EpisodeBackendCapabilities
    request_schema_hash: Digest = Field(default_factory=lambda: wire_digest(
        EpisodeInferenceRequest.model_json_schema()))
    reply_schema_hash: Digest = Field(default_factory=lambda: wire_digest(
        EpisodeInferenceReply.model_json_schema()))
    max_request_bytes: int = Field(default=262_144, ge=4096, le=262_144)
    max_reply_bytes: Literal[131_072] = 131_072
    max_inference_seconds: Literal[120] = 120
    provider_access_verified: Literal[False] = False


class EpisodeCapabilitiesRequest(StrictModel):
    schema_version: Literal["safe_agent.episode_capabilities_request.v1"] = (
        "safe_agent.episode_capabilities_request.v1"
    )
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    issued_at: datetime
    deadline: datetime

    _aware = field_validator("issued_at", "deadline")(require_aware)

    @model_validator(mode="after")
    def short_lifetime(self) -> "EpisodeCapabilitiesRequest":
        if not 0 < (self.deadline - self.issued_at).total_seconds() <= 15:
            raise ValueError("capability challenge must expire within fifteen seconds")
        return self

    @property
    def digest(self) -> str:
        return wire_digest(self.model_dump(mode="json"))


class EpisodeCapabilitiesRequestEnvelope(StrictModel):
    request: EpisodeCapabilitiesRequest
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class EpisodeCapabilitiesReply(StrictModel):
    schema_version: Literal["safe_agent.episode_capabilities_reply.v1"] = (
        "safe_agent.episode_capabilities_reply.v1"
    )
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: Digest
    issued_at: datetime
    deadline: datetime
    capabilities: EpisodeServiceCapabilities

    _aware = field_validator("issued_at", "deadline")(require_aware)


class EpisodeCapabilitiesReplyEnvelope(StrictModel):
    reply: EpisodeCapabilitiesReply
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


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

    def _capabilities_signature(self, purpose: str, value: StrictModel) -> str:
        # Domain separation prevents a readiness signature from authorizing an
        # inference dispatch, or a signed request from authenticating a reply.
        body = wire_json({"purpose": purpose, "payload": value.model_dump(mode="json")})
        return hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).hexdigest()

    def sign_capabilities_request(self, request: EpisodeCapabilitiesRequest) -> EpisodeCapabilitiesRequestEnvelope:
        return EpisodeCapabilitiesRequestEnvelope(request=request, signature=self._capabilities_signature(
            "safe_agent.episode_capabilities.request.v1", request))

    def verify_capabilities_request(self, envelope: EpisodeCapabilitiesRequestEnvelope) -> EpisodeCapabilitiesRequest:
        expected = self._capabilities_signature("safe_agent.episode_capabilities.request.v1", envelope.request)
        if not hmac.compare_digest(envelope.signature, expected):
            raise ValueError("EPISODE_CAPABILITIES_AUTHENTICATION_FAILED")
        return envelope.request

    def sign_capabilities_reply(self, reply: EpisodeCapabilitiesReply) -> EpisodeCapabilitiesReplyEnvelope:
        return EpisodeCapabilitiesReplyEnvelope(reply=reply, signature=self._capabilities_signature(
            "safe_agent.episode_capabilities.reply.v1", reply))

    def verify_capabilities_reply(self, envelope: EpisodeCapabilitiesReplyEnvelope) -> EpisodeCapabilitiesReply:
        expected = self._capabilities_signature("safe_agent.episode_capabilities.reply.v1", envelope.reply)
        if not hmac.compare_digest(envelope.signature, expected):
            raise ValueError("EPISODE_CAPABILITIES_AUTHENTICATION_FAILED")
        return envelope.reply
