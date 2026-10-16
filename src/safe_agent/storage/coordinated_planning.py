"""W4 transaction helpers. No inference transport or business writer lives here."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect, select

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.coordinated_planning import (
    MAX_PLANNING_INPUT_BYTES,
    CoordinatedPlanningInput,
)
from safe_agent.contracts.inference_results import ValidatedInferenceResult
from safe_agent.contracts.planning import PlanningLease, PlanningResultKind
from safe_agent.storage.coordinated_models import (
    CoordinatedPlanningConsumptionRow as ConsumptionRow,
)
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow as InputRow
from safe_agent.storage.inference_results import _read_record
from safe_agent.storage.models import InferenceInvocationRow, PlanningOutcomeRow
from safe_agent.storage.planning import _utc
from safe_agent.storage.repository import StateConflict, TaskTransaction
from safe_agent.storage.result_models import InferenceValidatedResultRow


def read_input(row: InputRow) -> CoordinatedPlanningInput:
    raw = row.record_json
    if not isinstance(raw, str) or not 0 < len(raw.encode("utf-8")) <= MAX_PLANNING_INPUT_BYTES:
        raise StateConflict("COORDINATED_INPUT_DENIED")
    value = CoordinatedPlanningInput.model_validate_json(raw)
    lease = value.lease
    if (canonical_json(value).decode("utf-8") != raw
            or (row.attempt_id, row.task_id, row.organization_id, row.manifest_hash,
                row.task_state_version, row.workspace_version, row.policy_hash, row.input_hash,
                _utc(row.recorded_at))
            != (lease.attempt_id, lease.task_id, value.context.organization_id, lease.manifest_hash,
                lease.task_state_version, lease.workspace_version, value.policy_hash, value.input_hash,
                _utc(value.projection.fetched_at))):
        raise StateConflict("COORDINATED_INPUT_DENIED")
    return value


def _installed(tx: TaskTransaction) -> bool:
    # Legacy migration acceptance deliberately operates at pre-W4 revisions.
    # The coordinated bootstrap separately REQUIRES both new tables; its own
    # reads/writes never fall back to a legacy path when a table is missing.
    return inspect(tx.session.connection()).has_table(InputRow.__tablename__)


def deny_coordinated_restart(tx: TaskTransaction) -> None:
    # Explicit retirement/replanning needs a later reviewed protocol. Expiry,
    # generic fail(), or switching planners cannot mint another coordinated call.
    if _installed(tx) and tx.session.scalar(select(InputRow.attempt_id).where(
        InputRow.task_id == tx.row.task_id,
    )) is not None:
        raise StateConflict("COORDINATED_ATTEMPT_REQUIRES_RESUME")


def bound_result(tx: TaskTransaction, value: CoordinatedPlanningInput) -> ValidatedInferenceResult:
    invocation = tx.session.scalar(select(InferenceInvocationRow).where(
        InferenceInvocationRow.task_id == value.lease.task_id,
        InferenceInvocationRow.attempt_id == value.lease.attempt_id,
        InferenceInvocationRow.organization_id == value.context.organization_id,
    ))
    if invocation is None or invocation.state != "RESPONSE_VALIDATED":
        raise StateConflict("COORDINATED_RESULT_PENDING_OR_UNCERTAIN")
    row = tx.session.get(InferenceValidatedResultRow, invocation.request_id)
    if row is None:
        raise StateConflict("COORDINATED_RESULT_MISSING")
    record = _read_record(row)
    if (record.lease != value.lease or record.policy_hash != value.policy_hash
            or record.request.task != value.context
            or record.request.availability != value.projection.to_planner_projection()
            or record.binding.binding_hash != invocation.binding_hash
            or record.validation_event_hash != invocation.latest_event_hash
            or record.outcome.proposal_digest != invocation.proposal_hash):
        raise StateConflict("COORDINATED_RESULT_BINDING_DENIED")
    return record


def _consumption_data(row: ConsumptionRow) -> dict[str, object]:
    return {"attempt_id": row.attempt_id, "request_id": row.request_id,
            "task_id": row.task_id, "input_hash": row.input_hash,
            "result_hash": row.result_hash, "candidate_hash": row.candidate_hash,
            "result_kind": row.result_kind, "proposal_hash": row.proposal_hash,
            "outcome_hash": row.outcome_hash, "recorded_at": _utc(row.recorded_at)}


def read_consumption(tx: TaskTransaction, value: CoordinatedPlanningInput) -> ConsumptionRow | None:
    row = tx.session.get(ConsumptionRow, value.lease.attempt_id)
    if row is None:
        return None
    record = bound_result(tx, value)
    outcome = tx.session.get(PlanningOutcomeRow, value.lease.attempt_id)
    if (sha256(_consumption_data(row)) != row.consumption_hash
            or row.task_id != value.lease.task_id or row.input_hash != value.input_hash
            or row.request_id != record.binding.request_id or row.result_hash != record.result_hash
            or row.candidate_hash != record.outcome.proposal_digest
            or outcome is None or outcome.outcome_hash != row.outcome_hash
            or outcome.result_kind != row.result_kind or outcome.proposal_hash != row.proposal_hash
            or _utc(outcome.recorded_at) != _utc(row.recorded_at)
            or not record.validated_at <= _utc(row.recorded_at) < record.binding.deadline_at):
        raise StateConflict("COORDINATED_CONSUMPTION_DENIED")
    if row.result_kind == "PROPOSED":
        if tx.snapshot.proposal_hash != row.proposal_hash or sha256(tx.read_active_proposal()) != row.proposal_hash:
            raise StateConflict("COORDINATED_FINAL_PROPOSAL_DENIED")
    elif row.result_kind != "NEEDS_CLARIFICATION" or row.proposal_hash is not None:
        raise StateConflict("COORDINATED_CONSUMPTION_DENIED")
    return row


def record_consumption(
    tx: TaskTransaction, lease: PlanningLease, *, result_hash: str | None,
    result_kind: PlanningResultKind, proposal_hash: str | None, now: datetime,
) -> None:
    """Tail of finish_planning_attempt, sharing its task lock and SQL commit.

    Legacy preparation has no result_hash and cannot finish a coordinated task.
    The final action must be the same candidate with only trusted simulation IDs
    filled in. The model never chooses the final action's approval digest.
    """
    if not _installed(tx):
        if result_hash is not None:
            raise StateConflict("COORDINATED_SCHEMA_REQUIRED")
        return
    row = tx.session.get(InputRow, lease.attempt_id)
    if row is None:
        if result_hash is not None:
            raise StateConflict("COORDINATED_INPUT_MISSING")
        return
    value = read_input(row)
    record = bound_result(tx, value)
    if (value.lease != lease or result_hash != record.result_hash
            or not record.validated_at <= now < record.binding.deadline_at
            or now >= value.projection.expires_at):
        raise StateConflict("COORDINATED_CONSUMPTION_DENIED")
    candidate = record.outcome.proposal
    outcome = tx.session.get(PlanningOutcomeRow, lease.attempt_id)
    if outcome is None:
        raise StateConflict("COORDINATED_OUTCOME_MISSING")
    if isinstance(candidate, CalendarCreateEvent):
        final = tx.read_active_proposal()
        if (result_kind != "PROPOSED" or not isinstance(final, CalendarCreateEvent)
                or final.simulation_ref == "simulation_pending"
                or final.projection_rev != value.projection.revision
                or candidate.model_copy(update={"simulation_ref": final.simulation_ref}) != final
                or sha256(final) != proposal_hash):
            raise StateConflict("COORDINATED_FINAL_PROPOSAL_DENIED")
    elif (not isinstance(candidate, RequestClarification)
          or result_kind != "NEEDS_CLARIFICATION" or proposal_hash is not None
          or outcome.reason_code != candidate.reason_code):
        raise StateConflict("COORDINATED_CLARIFICATION_DENIED")
    data = {"attempt_id": lease.attempt_id, "request_id": record.binding.request_id,
            "task_id": lease.task_id, "input_hash": value.input_hash,
            "result_hash": record.result_hash, "candidate_hash": record.outcome.proposal_digest,
            "result_kind": result_kind, "proposal_hash": proposal_hash,
            "outcome_hash": outcome.outcome_hash, "recorded_at": _utc(now)}
    tx.session.add(ConsumptionRow(**data, consumption_hash=sha256(data)))
    tx.session.flush()
