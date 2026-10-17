"""W3 closed, non-executable result evidence; never raw model text or secrets."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import RequestClarification
from safe_agent.contracts.base import Digest, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceOutcome, InferenceRequest
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationView,
    bind_planning_attempt,
)
from safe_agent.contracts.planning import PlanningLease

MAX_RESULT_BYTES = 65_536
SAFE_QUESTION = "Clarification required within the assigned task scope."


class ValidatedInferenceResult(StrictModel):
    """Trusted storage only. Digests detect corruption, not database-owner forgery.

    request contains the exact typed minimized task/free-busy snapshot, NOT the
    serialized prompt. outcome contains only a revalidated closed candidate.
    Its candidate digest is not the later simulated/final-action digest.
    """
    schema_version: Literal["safe_agent.validated_inference_result.v1"] = (
        "safe_agent.validated_inference_result.v1"
    )
    binding: InferenceInvocationBinding
    lease: PlanningLease
    policy_hash: Digest
    request: InferenceRequest = Field(repr=False)
    snapshot_hash: Digest
    outcome: InferenceOutcome = Field(repr=False)
    validation_event_hash: Digest
    validated_at: datetime
    result_hash: Digest

    @field_validator("validated_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def integrity(self) -> ValidatedInferenceResult:
        req = self.request
        result = self.outcome
        if (bind_planning_attempt(req, self.lease) != self.binding
                or self.snapshot_hash != sha256({"task": req.task, "availability": req.availability})
                or not req.issued_at <= self.validated_at < req.deadline_at
                or result.request_id != req.request_id or result.route_id != req.route_id
                or result.model_revision != req.model_revision
                or result.prompt_digest != req.prompt_digest
                or result.proposal_digest != sha256(result.proposal)):
            raise ValueError("INFERENCE_RESULT_BINDING_DENIED")
        if isinstance(result.proposal, RequestClarification) and result.proposal.question != SAFE_QUESTION:
            raise ValueError("INFERENCE_RESULT_UNSAFE_QUESTION")
        if sha256(self.model_dump(exclude={"result_hash"})) != self.result_hash:
            raise ValueError("INFERENCE_RESULT_HASH_MISMATCH")
        if len(canonical_json(self)) > MAX_RESULT_BYTES:
            raise ValueError("INFERENCE_RESULT_TOO_LARGE")
        return self


def seal_result(
    *, binding: InferenceInvocationBinding, lease: PlanningLease,
    policy_hash: str, request: InferenceRequest, outcome: InferenceOutcome,
    validation_event_hash: str, validated_at: datetime,
) -> ValidatedInferenceResult:
    # Recursive dumps rebuild children, rather than trusting model_copy bypasses.
    data = {
        "schema_version": "safe_agent.validated_inference_result.v1",
        "binding": binding.model_dump(), "lease": lease.model_dump(),
        "policy_hash": policy_hash, "request": request.model_dump(),
        "snapshot_hash": sha256({"task": request.task, "availability": request.availability}),
        "outcome": outcome.model_dump(), "validation_event_hash": validation_event_hash,
        "validated_at": require_aware(validated_at),
    }
    return ValidatedInferenceResult.model_validate({**data, "result_hash": sha256(data)})


class InferenceResultRecovery(StrictModel):
    """Trusted-local recovery receipt; never an authorization or freshness permit.

    The optional source snapshot is for the next trusted preparation step, not
    serialization to an operator/model endpoint. That step must check the real
    source and broker again. This module exposes no new HTTP route.
    """
    schema_version: Literal["safe_agent.inference_result_recovery.v1"] = (
        "safe_agent.inference_result_recovery.v1"
    )
    invocation: InferenceInvocationView
    status: Literal["AVAILABLE", "STALE", "LEGACY_MISSING", "NOT_VALIDATED"]
    result_hash: Digest | None = None
    outcome: InferenceOutcome | None = None
    source_snapshot: InferenceRequest | None = Field(default=None, repr=False, exclude=True)
    business_execution_authorized: Literal[False] = False
    billing_verified: Literal[False] = False
    dispatched_this_call: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> InferenceResultRecovery:
        if self.status == "AVAILABLE":
            if (self.invocation.state != "RESPONSE_VALIDATED"
                    or self.result_hash is None or self.outcome is None or self.source_snapshot is None
                    or self.outcome.request_id != self.invocation.request_id
                    or self.source_snapshot.request_id != self.invocation.request_id
                    or self.outcome.proposal_digest != self.invocation.proposal_hash
                    or self.outcome.prompt_digest != self.source_snapshot.prompt_digest):
                raise ValueError("INFERENCE_RESULT_RECOVERY_DENIED")
        elif self.outcome is not None or self.source_snapshot is not None:
            raise ValueError("INFERENCE_RESULT_NOT_REUSABLE")
        if (self.status in {"STALE", "LEGACY_MISSING"}
                and self.invocation.state != "RESPONSE_VALIDATED"):
            raise ValueError("INFERENCE_RESULT_RECOVERY_DENIED")
        if self.status == "NOT_VALIDATED" and self.invocation.state == "RESPONSE_VALIDATED":
            raise ValueError("INFERENCE_RESULT_RECOVERY_DENIED")
        return self
