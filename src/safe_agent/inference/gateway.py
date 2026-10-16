"""Offline fixture gateway: model output never becomes execution authority.

Application inference must use TrustedInferenceCoordinator with an authenticated
principal, actual planning lease, durable admission and coordinated transport.
This legacy stateless gateway now accepts ONLY the exact offline mock, even
when a budget ledger is supplied. It is not a fallback application dispatcher.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Literal

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import (
    CalendarCreateEvent,
    RequestClarification,
    parse_proposal_json,
)
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.inference import (
    InferenceOutcome,
    InferenceRequest,
    InferenceRoutePolicy,
    InferenceTransportReply,
)
from safe_agent.contracts.office import Projection
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.inference.transport import InferenceTransport
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger

_DENIAL_CODES = (
    "INVALID_INFERENCE_CONTEXT",
    "INFERENCE_SCOPE_DENIED",
    "INFERENCE_BUDGET_EXCEEDED",
    "INFERENCE_DEADLINE_EXCEEDED",
    "INFERENCE_BACKEND_UNAVAILABLE",
    "INFERENCE_RESPONSE_REJECTED",
)
DenialCode = Literal[
    "INVALID_INFERENCE_CONTEXT",
    "INFERENCE_SCOPE_DENIED",
    "INFERENCE_BUDGET_EXCEEDED",
    "INFERENCE_DEADLINE_EXCEEDED",
    "INFERENCE_BACKEND_UNAVAILABLE",
    "INFERENCE_RESPONSE_REJECTED",
]


class InferenceDenied(ValueError):
    """Fixed error code; never include model text, backend errors, or secrets."""

    def __init__(self, code: DenialCode) -> None:
        if code not in _DENIAL_CODES:
            raise ValueError("unrecognized inference denial code")
        super().__init__(code)
        self.code = code


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    collected: dict[str, object] = {}
    for name, value in pairs:
        if name in collected:
            raise InferenceDenied("INVALID_INFERENCE_CONTEXT")
        collected[name] = value
    return collected


def _forbid_constant(_: str) -> None:
    raise InferenceDenied("INVALID_INFERENCE_CONTEXT")


def _prompt_models(raw: str, *, max_bytes: int) -> tuple[SafeTaskContext, Projection, bytes]:
    if not isinstance(raw, str):
        raise InferenceDenied("INVALID_INFERENCE_CONTEXT")
    try:
        payload = raw.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InferenceDenied("INVALID_INFERENCE_CONTEXT") from None
    if not 0 < len(payload) <= max_bytes:
        raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED")
    try:
        data = json.loads(
            raw, object_pairs_hook=_unique_keys, parse_constant=_forbid_constant,
        )
        if (not isinstance(data, dict)
                or set(data) != {"instruction", "task", "availability"}
                or data["instruction"] != PLANNER_INSTRUCTION):
            raise InferenceDenied("INVALID_INFERENCE_CONTEXT")
        context = SafeTaskContext.model_validate_json(
            json.dumps(data["task"], allow_nan=False),
        )
        projection = Projection.model_validate_json(
            json.dumps(data["availability"], allow_nan=False),
        )
        expected = canonical_json({
            "instruction": PLANNER_INSTRUCTION,
            "task": context,
            "availability": projection,
        })
    except InferenceDenied:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InferenceDenied("INVALID_INFERENCE_CONTEXT") from None
    if expected != payload:
        raise InferenceDenied("INVALID_INFERENCE_CONTEXT")
    if (context.task_id != projection.task_id
            or context.organization_id != projection.organization_id
            or context.calendar_ref != projection.calendar_ref
            or context.simulation_ref != "simulation_pending"):
        raise InferenceDenied("INFERENCE_SCOPE_DENIED")
    if len(projection.free_slots) > 256:
        raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED")
    return context, projection, payload


def _div_ceiling(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _check_candidate(
    proposal: CalendarCreateEvent | RequestClarification,
    context: SafeTaskContext, projection: Projection,
) -> CalendarCreateEvent | RequestClarification:
    if isinstance(proposal, RequestClarification):
        # Model-generated questions are untrusted and must not become a UI
        # instruction, a stored prompt or an authority modification.
        return proposal.model_copy(update={
            "question": "Clarification required within the assigned task scope.",
        })
    if (proposal.calendar_ref != context.calendar_ref
            or proposal.customer_ref != context.customer_ref
            or proposal.template_id != context.template_id
            or proposal.projection_rev != projection.revision
            or proposal.simulation_ref != context.simulation_ref
            or proposal.time_zone != context.time_zone
            or proposal.start_at < context.allowed_start
            or proposal.end_at > context.allowed_end
            or proposal.end_at - proposal.start_at
               != timedelta(minutes=context.duration_minutes)
            or not any(
                slot.start_at <= proposal.start_at and
                proposal.end_at <= slot.end_at
                for slot in projection.free_slots
            )):
        raise InferenceDenied("INFERENCE_SCOPE_DENIED")
    return proposal


class InferenceGateway:
    """Stateless offline fixture compatibility, NOT application admission.

    A budget ledger does not supply a durable planning-attempt identity. Use
    TrustedInferenceCoordinator for the coordinated application API. Arbitrary
    Python callbacks and hosted/network transports are not accepted here.
    """

    def __init__(
        self, policy: InferenceRoutePolicy, transport: InferenceTransport, *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        budget_ledger: InferenceBudgetLedger | None = None,
        mode: Literal["offline_fixture"] = "offline_fixture",
    ) -> None:
        if mode != "offline_fixture" or type(transport) is not OfflineMockInferenceTransport:
            raise TypeError("offline fixture only; use coordinated inference admission")
        self._policy = policy
        self._transport = transport
        self._fixture_transport = transport
        self._clock = clock
        self._budget_ledger = budget_ledger

    @property
    def policy(self) -> InferenceRoutePolicy:
        """Trusted-local metadata; not an operator/model API route."""
        return self._policy

    def infer(self, prompt: str) -> InferenceOutcome:
        # Fail before parsing, reservations or any backend call if replaced.
        if (self._transport is not self._fixture_transport
                or type(self._transport) is not OfflineMockInferenceTransport):
            raise InferenceDenied("INFERENCE_SCOPE_DENIED")
        policy = self._policy
        context, projection, data = _prompt_models(
            prompt, max_bytes=min(policy.max_prompt_bytes, policy.max_prompt_token_proxy),
        )
        now = require_aware(self._clock())
        if projection.fetched_at > now or now >= projection.expires_at:
            raise InferenceDenied("INFERENCE_SCOPE_DENIED")
        max_reply_bytes = min(
            policy.max_response_bytes, policy.max_response_token_proxy,
        )
        upper_cost = (
            _div_ceiling(
                len(data) * policy.input_microusd_per_million_tokens, 1_000_000,
            )
            + _div_ceiling(
                max_reply_bytes * policy.output_microusd_per_million_tokens,
                1_000_000,
            )
        )
        if upper_cost > policy.max_estimated_cost_microusd:
            raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED")
        deadline = min(
            now + timedelta(seconds=policy.timeout_seconds),
            projection.expires_at,
        )
        if deadline <= now:
            raise InferenceDenied("INFERENCE_DEADLINE_EXCEEDED")
        request = InferenceRequest(
            request_id="inf_" + token_hex(16),
            route_id=policy.route_id, model_revision=policy.model_revision,
            prompt_digest=sha256({
                "instruction": PLANNER_INSTRUCTION,
                "task": context, "availability": projection,
            }),
            issued_at=now, deadline_at=deadline,
            task=context, availability=projection,
        )
        # Retain the existing optional accounting tests for the offline mock.
        budget = self._budget_ledger
        if budget is not None:
            try:
                budget.reserve(
                    request, max_microusd=upper_cost,
                    synthetic_mock=type(self._transport) is OfflineMockInferenceTransport,
                    now=now,
                )
            except InferenceBudgetDenied:
                raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED") from None
            except Exception:
                raise InferenceDenied("INFERENCE_BACKEND_UNAVAILABLE") from None

        validated = False
        reported = None
        try:
            try:
                # Recheck after trusted callbacks/storage; never fall back.
                if (self._transport is not self._fixture_transport
                        or type(self._transport) is not OfflineMockInferenceTransport):
                    raise InferenceDenied("INFERENCE_SCOPE_DENIED")
                raw = self._transport.infer(request)
            except Exception:
                # Neither the backend exception nor a raw model response is trusted
                # to be shown to the operator or recorded as a task event.
                raise InferenceDenied("INFERENCE_BACKEND_UNAVAILABLE") from None

            if require_aware(self._clock()) >= deadline:
                raise InferenceDenied("INFERENCE_DEADLINE_EXCEEDED")
            if type(raw) is not InferenceTransportReply:
                raise InferenceDenied("INFERENCE_RESPONSE_REJECTED")
            if (raw.request_id != request.request_id
                    or raw.route_id != request.route_id
                    or raw.model_revision != request.model_revision
                    or raw.prompt_digest != request.prompt_digest):
                raise InferenceDenied("INFERENCE_RESPONSE_REJECTED")
            try:
                output_bytes = raw.output_json.encode("utf-8", errors="strict")
            except UnicodeError:
                raise InferenceDenied("INFERENCE_RESPONSE_REJECTED") from None
            if not 0 < len(output_bytes) <= max_reply_bytes:
                raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED")
            if (raw.reported_usage is not None
                    and (raw.reported_usage.input_tokens > policy.max_prompt_token_proxy
                         or raw.reported_usage.output_tokens
                         > policy.max_response_token_proxy)):
                raise InferenceDenied("INFERENCE_BUDGET_EXCEEDED")
            reported = raw.reported_usage
            try:
                proposal = _check_candidate(
                    parse_proposal_json(raw.output_json), context, projection,
                )
            except InferenceDenied:
                raise
            except (ValueError, TypeError, UnicodeError):
                raise InferenceDenied("INFERENCE_RESPONSE_REJECTED") from None
            result = InferenceOutcome(
                request_id=request.request_id,
                route_id=request.route_id,
                model_revision=request.model_revision,
                prompt_digest=request.prompt_digest,
                proposal_digest=sha256(proposal),
                proposal=proposal,
                reported_usage=raw.reported_usage,
            )
            validated = True
            return result
        finally:
            if budget is not None:
                try:
                    at = require_aware(self._clock())
                    if (validated and self._transport is self._fixture_transport
                            and type(self._transport) is OfflineMockInferenceTransport):
                        budget.close_synthetic(
                            request.request_id, now=at, exact_offline_mock=True,
                        )
                    else:
                        # Failed/uncertain fixture calls keep their reservation.
                        # Coordinated remote-bound accounting is unchanged.
                        budget.mark_uncertain(
                            request.request_id, now=at,
                            reason="UNVERIFIED_RESULT" if validated else "UNKNOWN_EFFECT",
                            reported_usage=reported,
                        )
                except Exception:
                    raise InferenceDenied("INFERENCE_BACKEND_UNAVAILABLE") from None

    def generate(self, prompt: str) -> str:
        """Offline fixture callback only; not a coordinated application interface."""
        result = self.infer(prompt)
        return result.proposal.model_dump_json()
