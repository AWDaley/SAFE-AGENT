"""Atomic result persistence and authenticated, non-dispatching recovery.

No transport, refund, approval or business outbox exists in this module.
Recovery reuses the original typed input, never regenerates a fresh observation.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.inference import InferenceOutcome, InferenceRequest
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationView,
)
from safe_agent.contracts.inference_results import (
    MAX_RESULT_BYTES,
    InferenceResultRecovery,
    ValidatedInferenceResult,
    seal_result,
)
from safe_agent.contracts.planning import PlanningLease
from safe_agent.inference.gateway import _check_candidate, _div_ceiling, _prompt_models
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_admission import (
    InferenceAdmissionDenied,
    TrustedInferenceAdmission,
    _binding,
    _history,
    _live_attempt,
    _task_identity,
)
from safe_agent.storage.models import (
    InferenceAdmissionRow,
    InferenceInvocationRow,
    PlanningAttemptRow,
)
from safe_agent.storage.planning import _utc
from safe_agent.storage.repository import TaskTransaction
from safe_agent.storage.result_models import InferenceValidatedResultRow


class InferenceResultDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("INFERENCE_RESULT_DENIED")


def persist_validated_result(
    tx: TaskTransaction, *, binding: InferenceInvocationBinding, lease: PlanningLease,
    policy_hash: str, request: InferenceRequest, outcome: InferenceOutcome,
    view: InferenceInvocationView, now: datetime,
) -> None:
    """Called only inside the same locked transaction as RESPONSE_VALIDATED.

    No upsert: duplicate insertion rolls back. Existing results are loaded by
    recovery, never rewritten. SQL identity and append-only guards add defense.
    """
    if (view.state != "RESPONSE_VALIDATED" or view.request_id != binding.request_id
            or view.proposal_hash != outcome.proposal_digest):
        raise InferenceResultDenied()
    if _check_candidate(outcome.proposal, request.task, request.availability) != outcome.proposal:
        raise InferenceResultDenied()
    record = seal_result(binding=binding, lease=lease, policy_hash=policy_hash,
                         request=request, outcome=outcome, validation_event_hash=view.latest_event_hash,
                         validated_at=now)
    tx.session.add(InferenceValidatedResultRow(
        request_id=binding.request_id, binding_hash=binding.binding_hash,
        organization_id=binding.organization_id, task_id=binding.task_id,
        attempt_id=binding.attempt_id, policy_hash=policy_hash,
        candidate_hash=outcome.proposal_digest, validation_sequence=view.event_sequence,
        validation_event_hash=view.latest_event_hash, validated_at=now,
        result_hash=record.result_hash, record_json=canonical_json(record).decode("utf-8"),
    ))
    tx.session.flush()


def _read_record(row: InferenceValidatedResultRow) -> ValidatedInferenceResult:
    raw = row.record_json
    if not isinstance(raw, str) or not 0 < len(raw.encode("utf-8")) <= MAX_RESULT_BYTES:
        raise InferenceResultDenied()
    record = ValidatedInferenceResult.model_validate_json(raw)
    b = record.binding
    if (canonical_json(record).decode("utf-8") != raw
            or (row.request_id, row.binding_hash, row.organization_id, row.task_id, row.attempt_id,
                row.policy_hash, row.candidate_hash, row.validation_sequence,
                row.validation_event_hash, _utc(row.validated_at), row.result_hash)
            != (b.request_id, b.binding_hash, b.organization_id, b.task_id, b.attempt_id,
                record.policy_hash, record.outcome.proposal_digest, 3,
                record.validation_event_hash, record.validated_at, record.result_hash)):
        raise InferenceResultDenied()
    return record


def recover_validated_result(
    admission: TrustedInferenceAdmission, principal: Principal, lease: PlanningLease,
    *, expected_prompt: str | None = None,
) -> InferenceResultRecovery:
    """Inspect/recover one existing attempt; never admit or call a backend.

    AVAILABLE means a valid candidate at this read, not permission to execute.
    W4 must revalidate actual source revision and broker revocation at consumption.
    Invalid identity/integrity fails with a fixed code, not private SQL/model text.
    """
    try:
        actor = Principal.model_validate(principal.model_dump())
        planning = PlanningLease.model_validate(lease.model_dump())
        with admission._repo.write_task(planning.task_id, serialize_sqlite=True) as tx:
            now = require_aware(admission._clock()).astimezone(UTC)
            task, manifest = tx.task, tx.manifest
            actor.require("task:prepare", task.organization_id, now)
            actor.require("task:propose", task.organization_id, now)
            if (task.initiator_id != actor.actor_id or task.manifest_hash != sha256(manifest)
                    or task.task_id != planning.task_id or task.manifest_hash != planning.manifest_hash
                    or planning.mode != "text_only"):
                raise InferenceResultDenied()
            row = tx.session.scalar(select(InferenceInvocationRow).where(
                InferenceInvocationRow.organization_id == actor.organization_id,
                InferenceInvocationRow.task_id == planning.task_id,
                InferenceInvocationRow.attempt_id == planning.attempt_id,
            ))
            if row is None:
                raise InferenceResultDenied()
            bound = _binding(row)
            attempt = tx.session.get(PlanningAttemptRow, planning.attempt_id)
            admitted = tx.session.get(InferenceAdmissionRow, bound.request_id)
            if (attempt is None or admitted is None
                    or bound.task_state_version != planning.task_state_version
                    or bound.workspace_version != planning.workspace_version
                    or bound.lease_expires_at != planning.expires_at
                    or bound.manifest_hash != planning.manifest_hash
                    or attempt.task_id != planning.task_id or attempt.mode != planning.mode
                    or attempt.attempt_number != planning.attempt_number
                    or attempt.task_state_version != planning.task_state_version
                    or attempt.manifest_hash != planning.manifest_hash
                    or _utc(attempt.lease_until) != planning.expires_at
                    or now < _utc(attempt.started_at) or now < _utc(row.updated_at)
                    or admitted.binding_hash != bound.binding_hash
                    or admitted.policy_hash != admission._policy_hash):
                raise InferenceResultDenied()
            authority = admission._authority(tx)
            if now < _utc(authority.last_seen_at):
                raise InferenceResultDenied()
            authority.last_seen_at = now
            view = _history(tx, row, bound)
            stored = tx.session.get(InferenceValidatedResultRow, bound.request_id)
            if view.state != "RESPONSE_VALIDATED":
                if stored is not None:
                    raise InferenceResultDenied()
                return InferenceResultRecovery(invocation=view, status="NOT_VALIDATED")
            if stored is None:
                # Historical digest-only successes are NOT repaired by re-inference.
                return InferenceResultRecovery(invocation=view, status="LEGACY_MISSING")
            record = _read_record(stored)
            if (record.binding != bound or record.lease != planning
                    or record.policy_hash != admission._policy_hash
                    or record.validation_event_hash != view.latest_event_hash
                    or record.outcome.proposal_digest != view.proposal_hash):
                raise InferenceResultDenied()
            req = record.request
            prompt = canonical_json({"instruction": PLANNER_INSTRUCTION,
                                     "task": req.task, "availability": req.availability}).decode("utf-8")
            if expected_prompt is not None and expected_prompt != prompt:
                raise InferenceResultDenied()
            pol = admission._policy.route
            context, projection, data = _prompt_models(
                prompt, max_bytes=min(pol.max_prompt_bytes, pol.max_prompt_token_proxy),
            )
            upper = (_div_ceiling(len(data) * pol.input_microusd_per_million_tokens, 1_000_000)
                     + _div_ceiling(min(pol.max_response_bytes, pol.max_response_token_proxy)
                                    * pol.output_microusd_per_million_tokens, 1_000_000))
            intent = sha256({"policy_hash": admission._policy_hash, "lease": planning,
                             "prompt_hash": sha256(prompt), "max_microusd": upper})
            _task_identity(tx, actor, planning, context, now)
            admission._existing(tx, row, planning, context, projection, intent, upper)
            if _check_candidate(record.outcome.proposal, context, projection) != record.outcome.proposal:
                raise InferenceResultDenied()
            # A stale/consumed lease can be inspected, never revived by persistence.
            live = True
            try:
                _live_attempt(tx, planning, now)
            except InferenceAdmissionDenied:
                live = False
            end = require_aware(admission._clock()).astimezone(UTC)
            if end < now:
                raise InferenceResultDenied()
            if not live or end >= bound.deadline_at or end >= projection.expires_at:
                result = InferenceResultRecovery(invocation=view, status="STALE", result_hash=record.result_hash)
            else:
                result = InferenceResultRecovery(invocation=view, status="AVAILABLE",
                                                 result_hash=record.result_hash, outcome=record.outcome,
                                                 source_snapshot=req)
        # A slow commit is not evidence that the original result stayed fresh.
        final = require_aware(admission._clock()).astimezone(UTC)
        actor.require("task:prepare", actor.organization_id, final)
        actor.require("task:propose", actor.organization_id, final)
        if final < end:
            raise InferenceResultDenied()
        if result.status == "AVAILABLE" and final >= bound.deadline_at:
            return InferenceResultRecovery(invocation=view, status="STALE", result_hash=record.result_hash)
        return result
    except Exception:
        raise InferenceResultDenied() from None
