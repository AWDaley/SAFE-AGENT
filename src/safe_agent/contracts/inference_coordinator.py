"""Trusted-only offline dispatch contracts; never business execution authority."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import Digest, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceOutcome, InferenceRequest
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationView,
)


class InferenceDispatchClaim(StrictModel):
    schema_version: Literal["safe_agent.inference_dispatch_claim.v1"] = (
        "safe_agent.inference_dispatch_claim.v1"
    )
    binding: InferenceInvocationBinding
    claim_version: int = Field(ge=1)
    claim_until: datetime
    token: SecretStr = Field(repr=False, exclude=True)

    @field_validator("claim_until")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bounded(self) -> InferenceDispatchClaim:
        if not self.binding.issued_at < self.claim_until <= self.binding.deadline_at:
            raise ValueError("DISPATCH_CLAIM_DEADLINE_DENIED")
        if len(self.token.get_secret_value()) != 64:
            raise ValueError("DISPATCH_CLAIM_TOKEN_DENIED")
        return self


class CoordinatedInferenceEnvelope(StrictModel):
    """Inference-only message authenticated by a coordinator-exclusive key.

    A signature establishes the trusted caller, not current SQL authority. Only
    the coordinator may send it, once, after a committed DISPATCH_STARTED event.
    """
    schema_version: Literal["safe_agent.coordinated_inference_envelope.v1"] = (
        "safe_agent.coordinated_inference_envelope.v1"
    )
    request: InferenceRequest
    binding_hash: Digest
    dispatch_event_hash: Digest
    claim_version: int = Field(ge=1)
    signature: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class TrustedDispatchAuthenticator:
    """Host-only inference signing key; never part of the model payload."""
    def __init__(self, key: SecretStr) -> None:
        if type(key) is not SecretStr:
            raise TypeError("trusted dispatch secret required")
        raw = key.get_secret_value()
        if not 32 <= len(raw) <= 512 or not raw.isascii() or any(
            ord(c) < 33 or ord(c) > 126 for c in raw
        ):
            raise ValueError("dispatch secret format denied")
        self._key = raw.encode("ascii")

    def _signature(self, data: dict[str, object]) -> str:
        return hmac.new(self._key, b"SAFE-AGENT/OFFLINE-DISPATCH/v1\0" +
                        canonical_json(data), hashlib.sha256).hexdigest()

    def seal(self, request: InferenceRequest, claim: InferenceDispatchClaim,
             event_hash: str) -> CoordinatedInferenceEnvelope:
        req = InferenceRequest.model_validate(request.model_dump())
        if sha256(req) != claim.binding.request_digest:
            raise ValueError("DISPATCH_REQUEST_BINDING_DENIED")
        payload = {
            "schema_version": "safe_agent.coordinated_inference_envelope.v1",
            "request": request, "binding_hash": claim.binding.binding_hash,
            "dispatch_event_hash": event_hash, "claim_version": claim.claim_version,
        }
        envelope = CoordinatedInferenceEnvelope.model_validate({**payload, "signature": "0" * 64})
        digest = self._signature(envelope.model_dump(mode="json", exclude={"signature"}, exclude_none=True))
        return envelope.model_copy(update={"signature": digest})

    def verify(self, envelope: CoordinatedInferenceEnvelope) -> InferenceRequest:
        env = CoordinatedInferenceEnvelope.model_validate(envelope.model_dump())
        if not hmac.compare_digest(
            env.signature, self._signature(env.model_dump(mode="json", exclude={"signature"}, exclude_none=True)),
        ):
            raise ValueError("DISPATCH_ENVELOPE_DENIED")
        return env.request


class InferenceDispatchReceipt(StrictModel):
    schema_version: Literal["safe_agent.inference_dispatch_receipt.v1"] = (
        "safe_agent.inference_dispatch_receipt.v1"
    )
    invocation: InferenceInvocationView
    outcome: InferenceOutcome | None = None
    dispatched_this_call: bool = False
    business_execution_authorized: Literal[False] = False
    billing_verified: Literal[False] = False
    synthetic_only: Literal[True] = True

    @model_validator(mode="after")
    def bound(self) -> InferenceDispatchReceipt:
        if self.outcome is not None and (
            self.invocation.state != "RESPONSE_VALIDATED"
            or self.outcome.request_id != self.invocation.request_id
            or self.outcome.proposal_digest != self.invocation.proposal_hash
        ):
            raise ValueError("DISPATCH_RECEIPT_BINDING_DENIED")
        return self
