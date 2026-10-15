"""Invocation contract/replay evidence never admits a backend or a refund."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from safe_agent.canonical import sha256
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationEvent,
    bind_planning_attempt,
    replay_invocation,
    seal_invocation_event,
    validate_invocation_transition,
)
from safe_agent.contracts.planning import PlanningLease
from tests.m7.dispatch_fixtures import NOW, binding
from tests.m7.test_inference_sidecar import valid_request


def chain(*, terminal="UNKNOWN"):
    bound = binding()
    reserved = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    dispatched = seal_invocation_event(
        bound, state="DISPATCH_STARTED", previous=reserved,
        occurred_at=NOW + timedelta(seconds=1),
    )
    ended = seal_invocation_event(
        bound, state=terminal, previous=dispatched,
        occurred_at=NOW + timedelta(seconds=2),
        reason_code="DISPATCH_OUTCOME_UNKNOWN" if terminal == "UNKNOWN" else None,
        proposal_hash=sha256({"safe": "proposal"}) if terminal == "RESPONSE_VALIDATED" else None,
    )
    return bound, [reserved, dispatched, ended]


def test_binding_is_frozen_closed_and_contains_no_raw_input_or_authority():
    bound = binding()
    assert bound.binding_hash == InferenceInvocationBinding.model_validate_json(
        bound.model_dump_json()
    ).binding_hash
    for extra in ("api_key", "provider_url", "prompt", "output_json", "tools", "authorize"):
        with pytest.raises(ValidationError):
            InferenceInvocationBinding.model_validate({**bound.model_dump(), extra: "forbidden"})
    with pytest.raises(ValidationError):
        bound.task_id = "another_task"
    for value in (True, 1.5, "1", -1):
        with pytest.raises(ValidationError):
            InferenceInvocationBinding.model_validate({
                **bound.model_dump(), "task_state_version": value,
            })


@pytest.mark.parametrize("update", [
    {"deadline_at": NOW},
    {"deadline_at": NOW + timedelta(seconds=91)},
    {"deadline_at": NOW + timedelta(seconds=121), "lease_expires_at": NOW + timedelta(minutes=5)},
    {"issued_at": NOW.replace(tzinfo=None)},
    {"manifest_hash": "not_a_digest"},
])
def test_binding_rejects_invalid_deadlines_and_types(update):
    with pytest.raises(ValidationError):
        InferenceInvocationBinding.model_validate({**binding().model_dump(), **update})


def test_request_and_lease_are_revalidated_before_binding():
    req = valid_request()
    lease = PlanningLease(
        task_id=req.task.task_id, attempt_id="attempt_real_contract",
        workspace_version=1, attempt_number=1, task_state_version=0,
        manifest_hash=sha256({"synthetic": "manifest"}), mode="text_only",
        expires_at=req.issued_at + timedelta(seconds=90),
    )
    bound = bind_planning_attempt(req, lease)
    assert bound.request_digest == sha256(req)
    assert bound.attempt_id == lease.attempt_id
    assert bound.organization_id == req.task.organization_id
    for changed in (
        lease.model_copy(update={"task_id": "other_task"}),
        lease.model_copy(update={"mode": "deterministic"}),
        lease.model_copy(update={"expires_at": req.issued_at}),
        lease.model_copy(update={"workspace_version": True}),
    ):
        with pytest.raises(ValueError):
            bind_planning_attempt(req, changed)
    with pytest.raises(ValueError):
        bind_planning_attempt(req.model_copy(update={"prompt_digest": "bad"}), lease)


@pytest.mark.parametrize("terminal", ["UNKNOWN", "RESPONSE_VALIDATED"])
def test_replay_is_deterministic_and_neither_billing_nor_execution_evidence(terminal):
    bound, events = chain(terminal=terminal)
    first = replay_invocation(bound, events)
    restored = [InferenceInvocationEvent.model_validate_json(e.model_dump_json()) for e in events]
    assert first == replay_invocation(bound, restored)
    assert first.state == terminal and first.state_version == 2
    assert first.event_sequence == 3
    assert first.business_execution_authorized is False
    assert first.billing_verified is False
    assert first.synthetic_only is True
    for private in ("claim_owner", "attempt_id", "prompt_digest", "output_json", "held_microusd"):
        assert private not in first.model_dump()


@pytest.mark.parametrize("target", [
    "RESERVED", "DISPATCH_STARTED", "RESPONSE_VALIDATED", "FAILED_BEFORE_DISPATCH", "UNKNOWN",
])
def test_unknown_is_terminal_and_cannot_be_used_to_resend(target):
    with pytest.raises(ValueError, match="INVOCATION_TRANSITION_DENIED"):
        validate_invocation_transition("UNKNOWN", target)


@pytest.mark.parametrize("previous,target", [
    (None, "DISPATCH_STARTED"), ("RESERVED", "RESPONSE_VALIDATED"),
    ("RESERVED", "UNKNOWN"), ("DISPATCH_STARTED", "RESERVED"),
    ("DISPATCH_STARTED", "FAILED_BEFORE_DISPATCH"),
    ("RESPONSE_VALIDATED", "RESERVED"), ("FAILED_BEFORE_DISPATCH", "RESERVED"),
])
def test_skipped_states_rewinds_and_false_pre_dispatch_failures_are_denied(previous, target):
    with pytest.raises(ValueError):
        validate_invocation_transition(previous, target)


def test_replay_rejects_missing_reordered_tampered_and_cross_attempt_history():
    bound, events = chain()
    for invalid in ([], events[1:], list(reversed(events)), [events[0], events[0]],
                    [events[0], events[2]]):
        with pytest.raises(ValueError):
            replay_invocation(bound, invalid)
    for update in ({"event_hash": "sha256:" + "0" * 64},
                   {"reason_code": "RESPONSE_REJECTED"},
                   {"sequence": 7}):
        with pytest.raises(ValueError):
            replay_invocation(bound, [*events[:-1], events[-1].model_copy(update=update)])
    with pytest.raises(ValueError):
        replay_invocation(binding("b"), events)
    altered = bound.model_copy(update={"manifest_hash": sha256({"changed": True})})
    with pytest.raises(ValueError):
        replay_invocation(altered, events)


def test_late_success_clock_regression_and_untrusted_reason_are_denied():
    bound, events = chain()
    for at in (NOW - timedelta(seconds=1), bound.deadline_at):
        with pytest.raises(ValueError):
            seal_invocation_event(
                bound, state="RESPONSE_VALIDATED", previous=events[1],
                occurred_at=at, proposal_hash=sha256({"candidate": "safe"}),
            )
    with pytest.raises(ValueError):
        seal_invocation_event(bound, state="RESERVED", occurred_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        seal_invocation_event(bound, state="UNKNOWN", previous=events[1],
                              occurred_at=NOW + timedelta(seconds=3),
                              reason_code="RAW_PROVIDER_SECRET")
    unknown = seal_invocation_event(
        bound, state="UNKNOWN", previous=events[1],
        occurred_at=bound.deadline_at + timedelta(seconds=10),
        reason_code="DISPATCH_OUTCOME_UNKNOWN",
    )
    assert replay_invocation(bound, [*events[:2], unknown]).state == "UNKNOWN"


def test_pre_dispatch_failure_does_not_claim_free_or_successful_inference():
    bound = binding()
    reserved = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    failure = seal_invocation_event(
        bound, state="FAILED_BEFORE_DISPATCH", previous=reserved,
        occurred_at=bound.deadline_at, reason_code="AUTHORITY_EXPIRED",
    )
    view = replay_invocation(bound, [reserved, failure])
    assert view.state == "FAILED_BEFORE_DISPATCH"
    assert not view.billing_verified and not view.business_execution_authorized
