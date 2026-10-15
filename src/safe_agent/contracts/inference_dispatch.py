"""M7-05a: non-executable invocation identity and pure, fail-closed replay.

These contracts do not admit, dispatch, refund or authorize anything. A future
trusted SQL coordinator must bind them to current task/lease/budget authority.
Only digests and fixed reason codes are retained, never prompts or model text.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequest, InferenceRequestId
from safe_agent.contracts.planning import PlanningLease

InvocationState = Literal[
    "RESERVED", "DISPATCH_STARTED", "RESPONSE_VALIDATED", "FAILED_BEFORE_DISPATCH", "UNKNOWN",
]
InvocationReason = Literal[
    "AUTHORITY_EXPIRED", "CANCELLED_BEFORE_DISPATCH", "INFRASTRUCTURE_UNAVAILABLE",
    "DISPATCH_OUTCOME_UNKNOWN", "RESPONSE_REJECTED",
]
_TRANSITIONS: dict[InvocationState | None, frozenset[InvocationState]] = {
    None: frozenset({"RESERVED"}),
    "RESERVED": frozenset({"DISPATCH_STARTED", "FAILED_BEFORE_DISPATCH"}),
    "DISPATCH_STARTED": frozenset({"RESPONSE_VALIDATED", "UNKNOWN"}),
    "RESPONSE_VALIDATED": frozenset(),
    "FAILED_BEFORE_DISPATCH": frozenset(),
    "UNKNOWN": frozenset(),
}


def validate_invocation_transition(
    previous: InvocationState | None, target: InvocationState,
) -> None:
    """No rewind, resend, implicit success or automatic recovery from UNKNOWN."""
    if target not in _TRANSITIONS.get(previous, frozenset()):
        raise ValueError("INVOCATION_TRANSITION_DENIED")


class InferenceInvocationBinding(StrictModel):
    """Immutable, trusted-only identity; possession does not grant admission."""

    schema_version: Literal["safe_agent.inference_invocation_binding.v1"] = (
        "safe_agent.inference_invocation_binding.v1"
    )
    request_id: InferenceRequestId
    organization_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    task_state_version: int = Field(ge=0)
    workspace_version: int = Field(ge=1)
    manifest_hash: Digest
    route_id: Identifier
    model_revision: Identifier
    prompt_digest: Digest
    request_digest: Digest
    issued_at: datetime
    deadline_at: datetime
    lease_expires_at: datetime

    @field_validator("issued_at", "deadline_at", "lease_expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bounded_deadline(self) -> InferenceInvocationBinding:
        if not (
            self.issued_at < self.deadline_at <= self.lease_expires_at
            and self.deadline_at <= self.issued_at + timedelta(seconds=120)
        ):
            raise ValueError("INVOCATION_DEADLINE_DENIED")
        return self

    @property
    def binding_hash(self) -> str:
        return sha256(self)


def bind_planning_attempt(
    request: InferenceRequest, lease: PlanningLease,
) -> InferenceInvocationBinding:
    """Validate and identify a proposed binding, not its SQL admission.

    Revalidation rejects model_copy/model_construct bypasses. Actual lease
    ownership, tenant/task/manifest state and cost holds remain SQL concerns.
    """
    # Validate Python values before serialization can normalize bool-as-int.
    req = InferenceRequest.model_validate(request.model_dump())
    claim = PlanningLease.model_validate(lease.model_dump())
    if req.task.task_id != claim.task_id or claim.mode != "text_only":
        raise ValueError("INVOCATION_PLANNING_SCOPE_DENIED")
    if req.deadline_at > req.availability.expires_at:
        raise ValueError("INVOCATION_PROJECTION_EXPIRED")
    return InferenceInvocationBinding(
        request_id=req.request_id, organization_id=req.task.organization_id,
        task_id=req.task.task_id, attempt_id=claim.attempt_id,
        task_state_version=claim.task_state_version,
        workspace_version=claim.workspace_version, manifest_hash=claim.manifest_hash,
        route_id=req.route_id, model_revision=req.model_revision,
        prompt_digest=req.prompt_digest, request_digest=sha256(req),
        issued_at=req.issued_at, deadline_at=req.deadline_at,
        lease_expires_at=claim.expires_at,
    )


class InferenceInvocationEvent(StrictModel):
    """Hash-linked lifecycle metadata. Hash integrity is NOT authentication."""

    schema_version: Literal["safe_agent.inference_invocation_event.v1"] = (
        "safe_agent.inference_invocation_event.v1"
    )
    request_id: InferenceRequestId
    binding_hash: Digest
    sequence: int = Field(ge=1)
    previous_state: InvocationState | None
    state: InvocationState
    occurred_at: datetime
    reason_code: InvocationReason | None = None
    proposal_hash: Digest | None = None
    preceding_hash: Digest | None
    event_hash: Digest

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def lifecycle_and_integrity(self) -> InferenceInvocationEvent:
        validate_invocation_transition(self.previous_state, self.state)
        if ((self.sequence == 1) != (self.previous_state is None)
                or (self.sequence == 1) != (self.preceding_hash is None)):
            raise ValueError("INVOCATION_EVENT_ORDER_DENIED")
        if (self.state == "RESPONSE_VALIDATED") != (self.proposal_hash is not None):
            raise ValueError("INVOCATION_RESULT_METADATA_DENIED")
        reasons = {
            "FAILED_BEFORE_DISPATCH": {
                "AUTHORITY_EXPIRED", "CANCELLED_BEFORE_DISPATCH", "INFRASTRUCTURE_UNAVAILABLE",
            },
            "UNKNOWN": {"DISPATCH_OUTCOME_UNKNOWN", "RESPONSE_REJECTED"},
        }
        if self.state in reasons:
            if self.reason_code not in reasons[self.state]:
                raise ValueError("INVOCATION_REASON_DENIED")
        elif self.reason_code is not None:
            raise ValueError("INVOCATION_REASON_DENIED")
        if sha256(self.model_dump(exclude={"event_hash"})) != self.event_hash:
            raise ValueError("INVOCATION_EVENT_HASH_MISMATCH")
        return self


class InferenceInvocationView(StrictModel):
    """Safe lifecycle metadata. No lease owner, raw input, output or cost truth."""

    schema_version: Literal["safe_agent.inference_invocation_view.v1"] = (
        "safe_agent.inference_invocation_view.v1"
    )
    request_id: InferenceRequestId
    task_id: Identifier
    state: InvocationState
    state_version: int = Field(ge=0)
    event_sequence: int = Field(ge=1)
    latest_event_hash: Digest
    reason_code: InvocationReason | None = None
    proposal_hash: Digest | None = None
    business_execution_authorized: Literal[False] = False
    billing_verified: Literal[False] = False
    synthetic_only: Literal[True] = True


def _check_time(binding: InferenceInvocationBinding, event: InferenceInvocationEvent) -> None:
    if event.occurred_at < binding.issued_at:
        raise ValueError("INVOCATION_CLOCK_REGRESSION")
    if (event.state in {"RESERVED", "DISPATCH_STARTED", "RESPONSE_VALIDATED"}
            and event.occurred_at >= binding.deadline_at):
        raise ValueError("INVOCATION_DEADLINE_DENIED")


def seal_invocation_event(
    binding: InferenceInvocationBinding, *, state: InvocationState,
    occurred_at: datetime, previous: InferenceInvocationEvent | None = None,
    reason_code: InvocationReason | None = None, proposal_hash: str | None = None,
) -> InferenceInvocationEvent:
    """Construct deterministic evidence; no I/O, mutable state or authority."""
    occurred_at = require_aware(occurred_at)
    bound = InferenceInvocationBinding.model_validate(binding.model_dump())
    prior = (
        InferenceInvocationEvent.model_validate(previous.model_dump())
        if previous is not None else None
    )
    if prior is not None and (
        prior.request_id != bound.request_id or prior.binding_hash != bound.binding_hash
        or occurred_at < prior.occurred_at
    ):
        raise ValueError("INVOCATION_EVENT_BINDING_DENIED")
    payload = {
        "schema_version": "safe_agent.inference_invocation_event.v1",
        "request_id": bound.request_id, "binding_hash": bound.binding_hash,
        "sequence": 1 if prior is None else prior.sequence + 1,
        "previous_state": None if prior is None else prior.state,
        "state": state, "occurred_at": require_aware(occurred_at),
        "reason_code": reason_code, "proposal_hash": proposal_hash,
        "preceding_hash": None if prior is None else prior.event_hash,
    }
    event = InferenceInvocationEvent.model_validate({**payload, "event_hash": sha256(payload)})
    _check_time(bound, event)
    return event


def replay_invocation(
    binding: InferenceInvocationBinding, events: Sequence[InferenceInvocationEvent],
) -> InferenceInvocationView:
    """Pure replay. Never calls a model, retries dispatch or modifies a hold."""
    bound = InferenceInvocationBinding.model_validate(binding.model_dump())
    previous: InferenceInvocationEvent | None = None
    for sequence, raw in enumerate(events, start=1):
        event = InferenceInvocationEvent.model_validate(raw.model_dump())
        if (event.sequence != sequence or event.request_id != bound.request_id
                or event.binding_hash != bound.binding_hash
                or event.previous_state != (previous.state if previous else None)
                or event.preceding_hash != (previous.event_hash if previous else None)
                or (previous is not None and event.occurred_at < previous.occurred_at)):
            raise ValueError("INVOCATION_REPLAY_DENIED")
        _check_time(bound, event)
        previous = event
    if previous is None:
        raise ValueError("INVOCATION_HISTORY_MISSING")
    return InferenceInvocationView(
        request_id=bound.request_id, task_id=bound.task_id, state=previous.state,
        state_version=previous.sequence - 1, event_sequence=previous.sequence,
        latest_event_hash=previous.event_hash, reason_code=previous.reason_code,
        proposal_hash=previous.proposal_hash,
    )
