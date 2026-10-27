"""Read-only, aggregate diagnostics from private durable episode evidence.

No native arguments, messages, runtime IDs, hashes, reviewer identities, or
exception bodies leave this module. Optional task/trial IDs come only from an
explicit bound execution index. Reading a receipt proves recorded evidence
presence, not the semantic correctness of the underlying authorization.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from pydantic import Field, TypeAdapter, field_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.episode_inference import (
    INFERENCE_FAILURE_REASONS,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    parse_candidate,
    strict_json,
    wire_digest,
)
from safe_agent.contracts.episodes import (
    BudgetDelta,
    EpisodeEvent,
    EpisodeEventKind,
    EpisodeSpec,
    EpisodeView,
    InferenceReservation,
    PreparedCall,
    RunSpec,
)
from safe_agent.evidence.episodes import replay_episode, require_budget

from .execution import load_episode_index
from .runner import _write_json

_TABLES = (
    "safe_episode_runs",
    "safe_episodes",
    "safe_episode_events",
    "safe_episode_inference_reservations",
    "safe_episode_inference_results",
    "safe_episode_outgoing_calls",
    "safe_episode_tool_results",
    "safe_episode_private_contexts",
    "safe_episode_model_attempts",
)
_INVALID = (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError)
_DIGEST_ADAPTER = TypeAdapter(Digest)
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)

# Only these reviewed labels may become output keys. An arbitrary stored string,
# even an uppercase one, can contain private data and is exported only as OTHER.
_REASONS = frozenset(
    """
    CUSTOMER_CONSENT_REQUIRED CUSTOMER_SOURCE_TEXT_BOUND_EXCEEDED
    DOMAIN_MISMATCH DOMAIN_TOOL_NOT_EXPOSED DOMAIN_DISCOVERY_TARGET_INVALID
    DOMAIN_TOOL_NOT_DISCOVERED DOMAIN_TOOL_NOT_UNLOCKED DOMAIN_USER_TOOL_TARGET_INVALID
    DOMAIN_ARGUMENT_SCHEMA_INVALID DOMAIN_CUSTOMER_ALREADY_IDENTIFIED
    DOMAIN_CUSTOMER_IDENTITY_INPUT_REQUIRED DOMAIN_SUBJECT_MISMATCH
    DOMAIN_ORDER_OWNERSHIP_REQUIRED DOMAIN_ORDER_OBSERVATION_REQUIRED
    DOMAIN_ORDER_STATUS_INVALID DOMAIN_CANCELLATION_REASON_INVALID
    DOMAIN_ORDER_ITEMS_OBSERVATION_INVALID DOMAIN_ORDER_ITEMS_INVALID
    DOMAIN_REPLACEMENT_COUNT_INVALID DOMAIN_REPLACEMENT_VARIANT_UNVERIFIED
    DOMAIN_PRICE_OBSERVATION_REQUIRED DOMAIN_PAYMENT_OWNERSHIP_REQUIRED
    DOMAIN_PAYMENT_OBSERVATION_INVALID DOMAIN_REFUND_METHOD_INVALID
    DOMAIN_PAYMENT_CHANGE_INVALID DOMAIN_GIFT_CARD_BALANCE_INSUFFICIENT
    DOMAIN_BALANCE_OBSERVATION_REQUIRED DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED
    DOMAIN_CUSTOMER_RECORD_REQUIRED DOMAIN_VERIFICATION_RECORD_MISMATCH
    DOMAIN_TWO_CUSTOMER_IDENTITY_FIELDS_REQUIRED DOMAIN_OBSERVED_TIME_REQUIRED
    DOMAIN_GRANT_HASH_INVALID DOMAIN_PROPOSAL_OR_EVIDENCE_INVALID
    DOMAIN_BINDING_INVALID DOMAIN_OBSERVATION_STALE DOMAIN_OPERATION_NOT_GRANTED
    DOMAIN_DISPATCH_TARGET_NOT_GRANTED DOMAIN_EFFECTIVE_OPERATION_MISMATCH
    DOMAIN_MECHANICAL_CHECK_FAILED DOMAIN_SUBJECT_REQUIRED DOMAIN_AUTHENTICATION_REQUIRED
    DOMAIN_TOOL_BUDGET_EXHAUSTED DOMAIN_RECONCILIATION_REQUIRED
    DOMAIN_CUSTOMER_CONFIRMATION_REQUIRED DOMAIN_CUSTOMER_CONFIRMATION_STALE
    PRIVATE_REFERENCE_OPERATION_NOT_REGISTERED PRIVATE_REFERENCE_SCOPE_DENIED
    UNKNOWN_PRIVATE_REFERENCE PRIVATE_REFERENCE_KIND_OR_OPERATION_DENIED
    PRIVATE_ARGUMENT_REQUIRES_REFERENCE EMBEDDED_PRIVATE_REFERENCE_DENIED
    INVALID_NESTED_ARGUMENTS RECIPIENT_NOT_AUTHENTICATED
    PRIVATE_RESPONSE_VALUE_NOT_SCALAR UNRESOLVED_PRIVATE_RESPONSE_REFERENCE
    STOP_REQUIRES_FINISH_INTENT RESPONSE_EVIDENCE_NOT_FOUND SUCCESS_REQUIRES_TOOL_RESULT
    SUCCESS_REQUIRES_MUTATION_RESULT CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE
    EPISODE_INFERENCE_SCHEMA_REJECTED
    REPEATED_REPAIR_WITHOUT_PROGRESS REPAIR_ALLOWANCE_EXHAUSTED
    """.split()
)


class DiagnosticsError(ValueError):
    """Fixed public error codes; never wrap a private database exception body."""


class DenialReview(StrictModel):
    schema_version: Literal["safe_agent.denial_review.v1"]
    episode_id: Identifier
    diagnostic_index: int = Field(ge=0)
    proposal_hash: Digest
    reviewed: Literal[True]
    reviewer_id: Identifier
    reviewed_at: datetime
    label: Literal["false_refusal", "correct_denial", "not_assessable"]

    _aware = field_validator("reviewed_at")(require_aware)


@dataclass
class _Totals:
    counts: Counter[str] = field(default_factory=Counter)
    states: Counter[str] = field(default_factory=Counter)
    attempts: Counter[str] = field(default_factory=Counter)
    sources: Counter[str] = field(default_factory=Counter)
    denials: Counter[str] = field(default_factory=Counter)
    repairs: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    candidates: Counter[str] = field(default_factory=Counter)
    uncertainty_reasons: Counter[str] = field(default_factory=Counter)
    maxima: Counter[str] = field(default_factory=Counter)
    inference_seconds: list[float] = field(default_factory=list)
    tool_seconds: list[float] = field(default_factory=list)
    episode_seconds: list[float] = field(default_factory=list)
    # Private join keys are used only to bind reviewed annotations, never exported.
    reviewable: dict[tuple[str, int], str] = field(default_factory=dict)


def _merge_totals(target: _Totals, source: _Totals) -> None:
    for name in ("counts", "states", "attempts", "sources", "denials", "repairs", "failures", "candidates", "uncertainty_reasons"):
        getattr(target, name).update(getattr(source, name))
    for name in ("inference_seconds", "tool_seconds", "episode_seconds"):
        getattr(target, name).extend(getattr(source, name))
    for name, value in source.maxima.items():
        target.maxima[name] = max(target.maxima[name], value)
    target.reviewable.update(source.reviewable)


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("DURABLE_RECORD_BINDING_INVALID")


def _rows(db: sqlite3.Connection, table: str, episode_id: str) -> list[sqlite3.Row]:
    if table not in _TABLES:
        raise ValueError("UNKNOWN_DIAGNOSTICS_TABLE")
    return list(db.execute(f"SELECT * FROM {table} WHERE episode_id = ?", (episode_id,)))


def _events_and_view(
    row: sqlite3.Row, records: list[sqlite3.Row]
) -> tuple[EpisodeView, dict[EpisodeEventKind, dict[str, EpisodeEvent]]]:
    spec = EpisodeSpec.model_validate_json(row["spec_json"])
    view = EpisodeView.model_validate_json(row["snapshot_json"])
    _require(
        sha256(spec) == row["spec_hash"]
        and spec.episode_id == row["episode_id"]
        and spec.run_id == row["run_id"]
        and view.state_version == row["state_version"]
        and all(
            getattr(view, key) == getattr(spec, key)
            for key in (
                "episode_id",
                "run_id",
                "actor_id",
                "domain",
                "policy_hash",
                "limits",
                "created_at",
                "deadline",
            )
        )
    )
    events = []
    indexed: dict[EpisodeEventKind, dict[str, EpisodeEvent]] = {}
    for record in sorted(records, key=lambda item: item["sequence"]):
        event = EpisodeEvent.model_validate_json(record["record_json"])
        _require(
            event.episode_id == row["episode_id"]
            and event.sequence == record["sequence"]
            and event.kind.value == record["kind"]
            and event.event_hash == record["event_hash"]
        )
        correlation = event.payload.get("call_id") or event.payload.get("request_id")
        if correlation is not None:
            bucket = indexed.setdefault(event.kind, {})
            _require(record["external_id"] == correlation and correlation not in bucket)
            bucket[correlation] = event
        events.append(event)
    _require(replay_episode(events) == view)
    return view, indexed


def _tool_diagnostics(
    db: sqlite3.Connection,
    view: EpisodeView,
    events: dict[EpisodeEventKind, dict[str, EpisodeEvent]],
    totals: _Totals,
) -> None:
    prepared = events.get(EpisodeEventKind.CALL_PREPARED, {})
    completed = events.get(EpisodeEventKind.CALL_RESULT, {})
    uncertain = events.get(EpisodeEventKind.CALL_UNCERTAIN, {})
    records = {
        row["call_id"]: row for row in _rows(db, "safe_episode_outgoing_calls", view.episode_id)
    }
    results = {
        row["call_id"]: row for row in _rows(db, "safe_episode_tool_results", view.episode_id)
    }
    totals.counts.update(
        tool_prepared=len(prepared),
        tool_completed=len(completed),
        tool_pending=len(set(prepared) - set(completed)),
        tool_uncertain_ever=len(uncertain),
        tool_uncertain_unresolved=len(set(uncertain) - set(completed)),
        tool_resolved_after_uncertainty=len(set(uncertain) & set(completed)),
    )
    totals.failures["tool_record_set_mismatch"] += len(set(records) ^ set(prepared))
    totals.failures["tool_result_set_mismatch"] += len(set(results) ^ set(completed))
    for call_id, event in prepared.items():
        totals.counts["authorizations_checked"] += 1
        try:
            row = records[call_id]
            receipt = PreparedCall.model_validate_json(row["record_json"])
            call, auth = receipt.call, receipt.authorization
            _require(
                sha256(receipt) == row["record_hash"]
                and receipt.episode_id == view.episode_id == auth.episode_id
                and call.call_id == call_id
                and row["action_hash"] == call.action_hash == auth.action_hash
                and event.payload["action_hash"] == call.action_hash
                and event.payload["authorization_hash"] == sha256(auth)
                and call.policy_hash == auth.policy_hash == view.policy_hash
                and event.payload["observation_hash"]
                == call.observation_hash
                == auth.observation_hash
                and auth.consent_hash == call.consent_hash
                and auth.allowed is True
                and auth.state_version == event.state_version - 1
                and receipt.state_version == event.state_version
                and receipt.prepared_at == event.occurred_at
                and not receipt.emit_allowed
                and not receipt.uncertain
                and receipt.success is None
                and receipt.result_hash is None
            )
        except _INVALID:
            totals.failures["tool_authorization_binding_invalid"] += 1
        else:
            totals.counts["authorizations_verified"] += 1
            totals.counts["domain_receipts_present"] += auth.domain_admission_hash is not None
            totals.counts["consent_receipts_present"] += auth.consent_hash is not None
    for call_id, event in completed.items():
        totals.counts[
            "tool_successful" if event.payload["success"] == "true" else "tool_failed"
        ] += 1
        totals.tool_seconds.append(
            (event.occurred_at - prepared[call_id].occurred_at).total_seconds()
        )
        try:
            row = results[call_id]
            _require(
                row["result_hash"] == event.payload["result_hash"]
                and row["success"] in (0, 1)
                and bool(row["success"]) == (event.payload["success"] == "true")
            )
        except _INVALID:
            totals.failures["tool_result_binding_invalid"] += 1


def _inference_diagnostics(
    db: sqlite3.Connection,
    view: EpisodeView,
    events: dict[EpisodeEventKind, dict[str, EpisodeEvent]],
    totals: _Totals,
) -> None:
    reserved = events.get(EpisodeEventKind.INFERENCE_RESERVED, {})
    finished = events.get(EpisodeEventKind.INFERENCE_FINISHED, {})
    uncertain = events.get(EpisodeEventKind.INFERENCE_UNCERTAIN, {})
    reservations = {
        row["request_id"]: row
        for row in _rows(db, "safe_episode_inference_reservations", view.episode_id)
    }
    results = {
        row["request_id"]: row
        for row in _rows(db, "safe_episode_inference_results", view.episode_id)
    }
    attempts = {
        row["request_id"]: row for row in _rows(db, "safe_episode_model_attempts", view.episode_id)
    }
    totals.counts.update(
        model_reserved=len(reserved),
        model_finished=len(finished),
        model_unfinished=len(set(reserved) - set(finished)),
        model_uncertain=len(set(uncertain) - set(finished)),
        model_reservation_without_attempt=len(set(reserved) - set(attempts)),
        model_attempt_records=len(attempts),
        held_tokens=view.usage.tokens,
        held_cost_microusd=view.usage.cost_microusd,
    )
    totals.failures["inference_reservation_set_mismatch"] += len(set(reservations) ^ set(reserved))
    totals.failures["inference_result_set_mismatch"] += len(set(results) ^ set(finished))
    known_reasons = {sha256(reason): reason for reason in INFERENCE_FAILURE_REASONS | {"HARNESS_STOP_PENDING_MODEL"}}
    for event in uncertain.values():
        totals.uncertainty_reasons[known_reasons.get(event.payload.get("reason_hash") or "", "UNKNOWN_OR_LEGACY_REASON")] += 1
    for request_id, event in reserved.items():
        try:
            row = reservations[request_id]
            reservation = InferenceReservation.model_validate_json(row["record_json"])
            _require(
                sha256(reservation) == row["record_hash"]
                and reservation.episode_id == view.episode_id
                and reservation.run_id == view.run_id
                and reservation.request_id == request_id
                and reservation.input_hash == event.payload["input_hash"]
                and reservation.budget
                == BudgetDelta.model_validate_json(event.payload["budget_json"])
                and reservation.state_version == event.state_version
                and reservation.created_at == event.occurred_at
                and reservation.result_hash is None
                and not reservation.uncertain
            )
        except _INVALID:
            totals.failures["inference_reservation_binding_invalid"] += 1
    for request_id, event in finished.items():
        totals.inference_seconds.append(
            (event.occurred_at - reserved[request_id].occurred_at).total_seconds()
        )
        try:
            _require(results[request_id]["result_hash"] == event.payload["result_hash"])
        except _INVALID:
            totals.failures["inference_result_binding_invalid"] += 1
    repeated: Counter[str] = Counter()
    previous_message = None
    consecutive = 0
    for request_id, row in sorted(attempts.items(), key=lambda item: reserved[item[0]].sequence if item[0] in reserved else -1):
        totals.counts["model_attempts_checked"] += 1
        try:
            request = EpisodeInferenceRequest.model_validate_json(row["request_json"])
            reply = (
                EpisodeInferenceReply.model_validate_json(row["reply_json"])
                if row["reply_json"]
                else None
            )
            status = row["status"]
            _require(
                request.request_id == request_id
                and request.episode_id == view.episode_id
                and request.domain == view.domain
                and request.policy_hash == view.policy_hash
                and view.created_at <= request.issued_at < request.deadline <= view.deadline
                and request.digest
                == row["input_hash"]
                == reserved[request_id].payload["input_hash"]
                and status in {"PREPARED", "DISPATCHING", "COMPLETE", "UNCERTAIN"}
                and (status == "COMPLETE") == (reply is not None)
                and (reply is not None or row["reply_hash"] is None)
            )
            if reply is not None:
                _require(
                    reply.digest == row["reply_hash"]
                    and reply.request_id == request_id
                    and reply.input_hash == request.digest
                    and reply.model == request.model
                    and (
                        request_id not in finished
                        or reply.digest == finished[request_id].payload["result_hash"]
                    )
                )
            elif request_id in finished:
                raise ValueError("FINISHED_INFERENCE_REPLY_MISSING")
        except _INVALID:
            totals.failures["model_attempt_binding_invalid"] += 1
            continue
        totals.attempts[status] += 1
        omitted = request.projected_context.get("omitted_history_messages")
        if type(omitted) is int and omitted >= 0:
            totals.counts["requests_with_history_omission_metadata"] += 1
            totals.counts["requests_omitting_history"] += omitted > 0
            totals.counts["sum_omitted_history_messages_across_requests"] += omitted
        else:
            totals.counts["requests_without_history_omission_metadata"] += 1
        feedback = request.projected_context.get("feedback")
        if isinstance(feedback, (dict, list)):
            totals.counts["requests_with_repair_feedback"] += bool(feedback)
        if reply is not None:
            totals.counts["model_known_replies"] += 1
            totals.counts["model_known_reply_without_finish"] += request_id not in finished
            totals.counts["input_tokens"] += reply.input_tokens
            totals.counts["output_tokens"] += reply.output_tokens
            totals.sources[reply.usage_source] += 1
            try:
                candidate = parse_candidate(reply.output_json)
            except _INVALID:
                totals.counts["model_schema_rejected"] += 1
                previous_message, consecutive = None, 0
            else:
                totals.counts["model_schema_accepted"] += 1
                totals.candidates[candidate.kind] += 1
                if candidate.kind == "message":
                    totals.candidates["message_purpose_" + candidate.purpose] += 1
                    digest = wire_digest(candidate.text)
                    repeated[digest] += 1
                    consecutive = consecutive + 1 if digest == previous_message else 1
                    previous_message = digest
                    totals.maxima["identical_accepted_message_candidates_in_one_episode"] = max(
                        totals.maxima["identical_accepted_message_candidates_in_one_episode"], repeated[digest]
                    )
                    totals.maxima["consecutive_identical_accepted_message_candidates"] = max(
                        totals.maxima["consecutive_identical_accepted_message_candidates"], consecutive
                    )
                else:
                    previous_message, consecutive = None, 0
        else:
            previous_message, consecutive = None, 0


def _context_diagnostics(db: sqlite3.Connection, episode_id: str, totals: _Totals) -> None:
    records = _rows(db, "safe_episode_private_contexts", episode_id)
    if not records:
        totals.counts["contexts_missing"] += 1
        return
    totals.counts["contexts_checked"] += 1
    try:
        _require(len(records) == 1)
        row = records[0]
        context = strict_json(row["context_json"], max_bytes=32 * 1024 * 1024)
        _require(isinstance(context, dict) and wire_digest(context) == row["context_hash"])
        assert isinstance(context, dict)
        _require(context.get("episode_id") == episode_id)
        diagnostics = context.get("diagnostics", [])
        _require(isinstance(diagnostics, list))
    except _INVALID:
        totals.failures["private_context_integrity_invalid"] += 1
        return
    totals.counts["contexts_verified"] += 1
    totals.counts["contexts_processing_claim_active"] += row["active_observation"] is not None
    for index, item in enumerate(diagnostics):
        if not isinstance(item, dict) or not isinstance(item.get("code"), str):
            totals.counts["diagnostic_entries_unclassified"] += 1
            continue
        reason = item["code"] if item["code"] in _REASONS else "OTHER"
        if item.get("repair") is True:
            totals.repairs[reason] += 1
        elif isinstance(item.get("proposal_hash"), str):
            # Parse the digest without copying the private diagnostic into output.
            try:
                proposal_hash = _DIGEST_ADAPTER.validate_python(item["proposal_hash"])
            except _INVALID:
                totals.counts["diagnostic_entries_unclassified"] += 1
                continue
            totals.denials[reason] += 1
            totals.reviewable[(episode_id, index)] = proposal_hash
        else:
            totals.counts["diagnostic_entries_unclassified"] += 1


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "min": min(ordered) if ordered else None,
        "mean": fmean(ordered) if ordered else None,
        "p50": ordered[math.ceil(len(ordered) * 0.50) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "p99": ordered[math.ceil(len(ordered) * 0.99) - 1] if ordered else None,
        "max": max(ordered) if ordered else None,
    }


def _review_counts(path: Path | None, totals: _Totals) -> dict[str, Any]:
    labels: Counter[str] = Counter()
    reviewed: set[tuple[str, int]] = set()
    if path is not None:
        try:
            with path.open() as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    raw = strict_json(line, max_bytes=8192)
                    _require(isinstance(raw, dict) and raw.get("reviewed") is True)
                    review = DenialReview.model_validate_json(line)
                    key = (review.episode_id, review.diagnostic_index)
                    _require(
                        key not in reviewed and totals.reviewable.get(key) == review.proposal_hash
                    )
                    reviewed.add(key)
                    labels[review.label] += 1
        except (OSError, *_INVALID):
            raise DiagnosticsError("INVALID_REVIEW_ANNOTATIONS") from None
    assessable = labels["false_refusal"] + labels["correct_denial"]
    population = sum(totals.denials.values())
    rate = labels["false_refusal"] / assessable if assessable else None
    return {
        "unit": "recorded domain proposal denials; not user requests or benchmark tasks",
        "reviewed": len(reviewed),
        "unreviewed": population - len(reviewed),
        "false_refusal": labels["false_refusal"],
        "correct_denial": labels["correct_denial"],
        "not_assessable": labels["not_assessable"],
        "reviewed_assessable_denial_rate": rate,
        "all_recorded_denial_rate": rate if assessable == population and population else None,
        "status": (
            "reviewed_complete"
            if reviewed and len(reviewed) == population
            else "reviewed_subset"
            if reviewed
            else "unknown_without_reviewed_annotations"
        ),
    }


def _report(totals: _Totals, annotations: Path | None, filtered: bool) -> dict[str, Any]:
    n = totals.counts
    failures = {key: value for key, value in sorted(totals.failures.items()) if value}
    return {
        "schema_version": "safe_agent.tau3_diagnostics.v1",
        "kind": "aggregate_runtime_diagnostics",
        "official_rewards_modified": False,
        "source": "one read-only SQLite snapshot; fixed labels and aggregates only",
        "run_filter_applied": filtered,
        "episodes": {
            "recorded": n["episodes_recorded"],
            "replay_verified": n["episodes_verified"],
            "by_state": dict(sorted(totals.states.items())),
            "private_contexts_verified": n["contexts_verified"],
            "private_contexts_missing": n["contexts_missing"],
            "processing_claims_active": n["contexts_processing_claim_active"],
        },
        "tool_calls": {
            "prepared": n["tool_prepared"],
            "completed": n["tool_completed"],
            "successful_results": n["tool_successful"],
            "failed_results": n["tool_failed"],
            "without_result": n["tool_pending"],
            "uncertain_ever": n["tool_uncertain_ever"],
            "uncertain_unresolved": n["tool_uncertain_unresolved"],
            "resolved_after_uncertainty": n["tool_resolved_after_uncertainty"],
            "actual_external_dispatches": None,
            "dispatch_evidence_limit": "a prepared outbox entry alone does not prove external execution",
        },
        "authorization": {
            "prepared_calls_checked": n["authorizations_checked"],
            "verified_persisted_bindings": n["authorizations_verified"],
            "missing_or_invalid_bindings": n["authorizations_checked"]
            - n["authorizations_verified"],
            "bindings_with_domain_admission_receipt": n["domain_receipts_present"],
            "bindings_without_domain_admission_receipt": n["authorizations_verified"]
            - n["domain_receipts_present"],
            "bindings_with_customer_consent": n["consent_receipts_present"],
            "semantic_authorization_reverified": False,
            "consent_requirement_reverified": False,
        },
        "denials": {
            "domain_proposals": sum(totals.denials.values()),
            "by_reason": dict(sorted(totals.denials.items())),
            "repair_feedback": sum(totals.repairs.values()),
            "repair_feedback_by_reason": dict(sorted(totals.repairs.items())),
            "unclassified_diagnostic_entries": n["diagnostic_entries_unclassified"],
            "scope": "persisted private-context diagnostics; interrupted unsaved diagnostics may be absent",
            "repair_is_domain_denial": False,
            "repair_is_proof_of_unsafe_action_prevented": False,
        },
        "model_attempts": {
            "reserved": n["model_reserved"],
            "journal_finished": n["model_finished"],
            "journal_unfinished": n["model_unfinished"],
            "journal_uncertain": n["model_uncertain"],
            "durable_attempt_records": n["model_attempt_records"],
            "verified_attempts_by_state": dict(sorted(totals.attempts.items())),
            "reservations_without_attempt_row": n["model_reservation_without_attempt"],
            "known_replies": n["model_known_replies"],
            "attempts_with_unknown_usage": n["model_attempt_records"] - n["model_known_replies"],
            "usage_complete_for_reserved_attempts": n["model_reserved"] == n["model_known_replies"] and not failures,
            "known_replies_without_journal_finish": n["model_known_reply_without_finish"],
            "schema_accepted_replies": n["model_schema_accepted"],
            "schema_rejected_replies": n["model_schema_rejected"],
            "recorded_input_tokens": n["input_tokens"],
            "recorded_output_tokens": n["output_tokens"],
            "usage_sources": dict(sorted(totals.sources.items())),
            "lifetime_reserved_tokens": n["held_tokens"],
            "lifetime_reserved_cost_microusd": n["held_cost_microusd"],
            "reserved_cost_is_actual_spend": False,
            "actual_billed_cost_usd": None,
            "uncertainty_by_fixed_reason": dict(sorted(totals.uncertainty_reasons.items())),
        },
        "behavioral_diagnostics": {
            "schema_accepted_candidates": dict(sorted(totals.candidates.items())),
            "max_identical_message_candidates_in_one_episode": totals.maxima["identical_accepted_message_candidates_in_one_episode"],
            "max_consecutive_identical_message_candidates": totals.maxima["consecutive_identical_accepted_message_candidates"],
            "requests_with_history_omission_metadata": n["requests_with_history_omission_metadata"],
            "requests_without_history_omission_metadata": n["requests_without_history_omission_metadata"],
            "requests_omitting_history": n["requests_omitting_history"],
            "sum_omitted_history_messages_across_requests": n["sum_omitted_history_messages_across_requests"],
            "requests_with_repair_feedback": n["requests_with_repair_feedback"],
            "role_mismatch_count": None,
            "role_adherence_assessed": False,
            "no_progress_terminations": {
                name: totals.repairs[name] for name in (
                    "REPEATED_REPAIR_WITHOUT_PROGRESS", "REPAIR_ALLOWANCE_EXHAUSTED"
                )
            },
            "interpretation": "schema acceptance and repetition describe structure only; role adherence, progress and repair false positives require separate review",
        },
        "latency_seconds": {
            "inference_reservation_to_finish": _distribution(totals.inference_seconds),
            "tool_prepare_to_result": _distribution(totals.tool_seconds),
            "episode_creation_to_last_journal_event": _distribution(totals.episode_seconds),
            "inference_without_endpoint": n["model_unfinished"],
            "tools_without_endpoint": n["tool_pending"],
            "method": "matched verified event timestamps; nearest-rank percentiles; incomplete pairs excluded",
            "scope": "host-observed intervals including orchestration and persistence, not isolated provider latency",
        },
        "integrity": {
            "episode_replay_checked": n["episodes_recorded"],
            "episode_replay_failures": totals.failures["episode_replay_invalid"],
            "private_contexts_checked": n["contexts_checked"],
            "model_attempts_checked": n["model_attempts_checked"],
            "failures_by_check": failures,
            "passed": not failures,
            "metrics_scope": "verified episode journals; invalid journals are excluded and remain counted as failures",
        },
        "false_refusals": _review_counts(annotations, totals),
    }


def diagnose_database(
    database: Path,
    *,
    run_id: str | None = None,
    annotations: Path | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect durable state without locking for writes, replaying actions, or judging."""
    totals = _Totals()
    episode_index = None
    if run_dir is not None:
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text())
            result_path = run_dir / "results.json"
            simulations = json.loads(result_path.read_text())["simulations"] if result_path.exists() else []
            if run_id is not None and run_id != manifest["run_id"]:
                raise ValueError
            run_id = manifest["run_id"]
            episode_index = load_episode_index(run_dir, manifest, simulations)
        except (OSError, TypeError, ValueError, KeyError):
            raise DiagnosticsError("DIAGNOSTICS_EPISODE_INDEX_INVALID") from None
    per_episode: dict[str, _Totals] = {}
    if run_id is not None:
        try:
            _IDENTIFIER_ADAPTER.validate_python(run_id)
        except _INVALID:
            raise DiagnosticsError("DIAGNOSTICS_RUN_FILTER_INVALID") from None
    try:
        path = database.resolve(strict=True)
        if not path.is_file():
            raise DiagnosticsError("DIAGNOSTICS_DATABASE_NOT_FOUND")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if not set(_TABLES) <= tables:
                raise DiagnosticsError("DIAGNOSTICS_SCHEMA_MISSING")
            run_rows = list(
                db.execute(
                    "SELECT * FROM safe_episode_runs"
                    + (" WHERE run_id = ?" if run_id is not None else ""),
                    (run_id,) if run_id is not None else (),
                )
            )
            if run_id is not None and not run_rows:
                raise DiagnosticsError("DIAGNOSTICS_RUN_NOT_FOUND")
            episode_rows = list(
                db.execute(
                    "SELECT * FROM safe_episodes"
                    + (" WHERE run_id = ?" if run_id is not None else ""),
                    (run_id,) if run_id is not None else (),
                )
            )
            totals.counts["episodes_recorded"] = len(episode_rows)
            known_runs = {row["run_id"] for row in run_rows}
            totals.failures["episode_run_missing"] += sum(
                row["run_id"] not in known_runs for row in episode_rows
            )
            verified: dict[str, list[EpisodeView]] = {}
            for row in episode_rows:
                try:
                    view, events = _events_and_view(
                        row, _rows(db, "safe_episode_events", row["episode_id"])
                    )
                except _INVALID:
                    totals.failures["episode_replay_invalid"] += 1
                    continue
                local = _Totals()
                local.counts["episodes_verified"] = 1
                local.states[view.state.value] += 1
                local.episode_seconds.append((view.updated_at - view.created_at).total_seconds())
                verified.setdefault(view.run_id, []).append(view)
                _tool_diagnostics(db, view, events, local)
                _inference_diagnostics(db, view, events, local)
                _context_diagnostics(db, view.episode_id, local)
                _merge_totals(totals, local)
                per_episode[view.episode_id] = local
            for row in run_rows:
                try:
                    spec = RunSpec.model_validate_json(row["spec_json"])
                    _require(spec.run_id == row["run_id"] and sha256(spec) == row["spec_hash"])
                    usage = BudgetDelta(**{name: row[name] for name in BudgetDelta.model_fields})
                    require_budget(usage, spec.limits)
                    _require(
                        row["state_version"] == usage.turns + usage.model_calls + usage.tool_calls
                    )
                    views = verified.get(spec.run_id, [])
                    _require(
                        all(
                            view.created_at >= spec.created_at
                            and view.deadline <= spec.deadline
                            and all(
                                getattr(view.limits, name) <= getattr(spec.limits, name)
                                for name in type(spec.limits).model_fields
                            )
                            for view in views
                        )
                    )
                    expected_episodes = sum(item["run_id"] == spec.run_id for item in episode_rows)
                    if len(views) == expected_episodes:
                        _require(
                            all(
                                getattr(usage, name)
                                == sum(getattr(view.usage, name) for view in views)
                                for name in BudgetDelta.model_fields
                            )
                        )
                except _INVALID:
                    totals.failures["run_aggregate_binding_invalid"] += 1
            if run_id is None:
                # Orphan rows cannot be attributed to a requested run. Count them
                # only when inspecting the entire database, without exporting IDs.
                for table in _TABLES[2:]:
                    count = db.execute(
                        f"SELECT count(*) FROM {table} AS r LEFT JOIN safe_episodes AS e "
                        "ON r.episode_id = e.episode_id WHERE e.episode_id IS NULL"
                    ).fetchone()[0]
                    totals.failures["orphan_durable_records"] += count
            db.rollback()
        finally:
            db.close()
    except FileNotFoundError:
        raise DiagnosticsError("DIAGNOSTICS_DATABASE_NOT_FOUND") from None
    except (sqlite3.Error, OSError):
        raise DiagnosticsError("DIAGNOSTICS_DATABASE_UNREADABLE") from None
    report = _report(totals, annotations, run_id is not None)
    report["task_attribution"] = {
        "available": episode_index is not None,
        "basis": "explicit hash-bound task/trial-to-episode index; never inferred from serial order",
        "bound_runtime_episodes": sum(row["episode_id"] is not None for row in episode_index["rows"]) if episode_index is not None else None,
        "verified_bound_episodes": sum(row["episode_id"] in per_episode for row in episode_index["rows"]) if episode_index is not None else None,
    }
    report["tasks"] = [
        _task_diagnostics(row, per_episode.get(row["episode_id"])) for row in episode_index["rows"]
    ] if episode_index is not None else None
    return report


def _task_diagnostics(row: dict[str, Any], totals: _Totals | None) -> dict[str, Any]:
    result = {
        "task_id": row["task_id"], "trial": row["trial"],
        "execution_state": row["state"], "graded": row["graded"],
        "termination_reason": row["termination_reason"],
        "episode_binding_available": row["episode_id"] is not None,
        "runtime_journal_verified": totals is not None,
        "adapter_counters": dict(row["counters"]),
        "episode_elapsed_to_last_journal_event_seconds": None,
        "runtime": None,
    }
    if totals is not None:
        summary = _report(totals, None, True)
        result["episode_elapsed_to_last_journal_event_seconds"] = totals.episode_seconds[0]
        result["runtime"] = {
            key: summary[key] for key in ("model_attempts", "tool_calls", "denials", "behavioral_diagnostics", "integrity")
        }
    return result


def report_diagnostics(
    database: Path,
    *,
    output: Path | None = None,
    run_id: str | None = None,
    annotations: Path | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    report = diagnose_database(database, run_id=run_id, annotations=annotations, run_dir=run_dir)
    if output is not None:
        target = output.resolve()
        if target == database.resolve() or (annotations and target == annotations.resolve()):
            raise DiagnosticsError("DIAGNOSTICS_OUTPUT_IS_SOURCE")
        if target.name in {
            "results.json",
            "manifest.json",
            "official_metrics.json",
            "additional_costs.jsonl",
            "episode-index.json",
            "progress.json",
        }:
            raise DiagnosticsError("DIAGNOSTICS_OUTPUT_IS_BENCHMARK_EVIDENCE")
        if str(target) in {
            str(database.resolve()) + suffix for suffix in ("-wal", "-shm", "-journal")
        }:
            raise DiagnosticsError("DIAGNOSTICS_OUTPUT_IS_SOURCE")
        try:
            _write_json(output, report)
        except OSError:
            raise DiagnosticsError("DIAGNOSTICS_OUTPUT_UNWRITABLE") from None
    return report
