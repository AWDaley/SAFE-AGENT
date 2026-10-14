"""Trusted bounded planning workspace and immutable, privacy-minimal attempt evidence.

No planner/model/provider interfaces reside in this storage layer. Only a trusted
preparation transaction may record a PROPOSED outcome; all other attempts
complete without changing the authoritative task state.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from secrets import token_hex

from sqlalchemy import select

from safe_agent.canonical import sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.planning import (
    PlanningLease,
    PlanningMode,
    PlanningReason,
    PlanningResultKind,
    PlanningWorkspaceView,
)
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.models import (
    PlanningAttemptRow,
    PlanningOutcomeRow,
    PlanningWorkspaceRow,
)
from safe_agent.storage.repository import LedgerRepository, StateConflict, TaskTransaction


def _utc(value: datetime) -> datetime:
    """SQLite stores UTC without an offset; PostgreSQL preserves it."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _assert_workspace(tx: TaskTransaction, row: PlanningWorkspaceRow) -> None:
    task = tx.task
    if (row.task_id != task.task_id or row.organization_id != task.organization_id
            or row.initiator_id != task.initiator_id
            or row.manifest_hash != task.manifest_hash
            or row.attempts_used < 0 or row.attempts_used > row.max_attempts
            or not 1 <= row.max_attempts <= 10 or row.workspace_version < 0):
        raise StateConflict("planning workspace is not bound to immutable task authority")


def _seal_outcome(
    tx: TaskTransaction, attempt: PlanningAttemptRow, *,
    result_kind: PlanningResultKind,
    reason_code: PlanningReason | None,
    proposal_hash: str | None,
    now: datetime,
) -> None:
    if tx.session.get(PlanningOutcomeRow, attempt.attempt_id) is not None:
        raise StateConflict("duplicate planning outcome")
    payload = {
        "attempt_id": attempt.attempt_id, "task_id": attempt.task_id,
        "attempt_number": attempt.attempt_number,
        "manifest_hash": attempt.manifest_hash,
        "task_state_version": attempt.task_state_version,
        "result_kind": result_kind, "reason_code": reason_code,
        "proposal_hash": proposal_hash, "recorded_at": now,
    }
    tx.session.add(PlanningOutcomeRow(
        attempt_id=attempt.attempt_id, task_id=attempt.task_id,
        result_kind=result_kind, reason_code=reason_code,
        proposal_hash=proposal_hash, recorded_at=now,
        outcome_hash=sha256(payload),
    ))


def finish_planning_attempt(
    tx: TaskTransaction, lease: PlanningLease, *,
    result_kind: PlanningResultKind,
    reason_code: PlanningReason | None,
    proposal_hash: str | None,
    now: datetime,
) -> None:
    """Complete under the SAME SQL task lock as any admitted proposal.

    The trusted coordinator calls this at the tail of its atomic preview,
    proposal and ledger transaction. A failed lease rolls the whole change back.
    """
    now = require_aware(now).astimezone(UTC)
    row = tx.session.get(PlanningWorkspaceRow, tx.row.task_id)
    attempt = tx.session.get(PlanningAttemptRow, lease.attempt_id)
    if (row is None or attempt is None
            or row.active_attempt_id != lease.attempt_id
            or row.workspace_version != lease.workspace_version
            or row.status != "RUNNING"
            or row.lease_until is None
            or _utc(row.lease_until) != lease.expires_at.astimezone(UTC)
            or attempt.task_id != tx.row.task_id
            or attempt.attempt_number != lease.attempt_number
            or attempt.task_state_version != lease.task_state_version
            or attempt.manifest_hash != lease.manifest_hash
            or attempt.mode != lease.mode
            or _utc(attempt.lease_until) != lease.expires_at.astimezone(UTC)):
        raise StateConflict("planning attempt lease no longer matches task")
    _assert_workspace(tx, row)
    if result_kind == "PROPOSED":
        if (now >= lease.expires_at
                or tx.snapshot.phase not in {
                    OfficePhase.READY_TO_COMMIT, OfficePhase.AWAITING_APPROVAL,
                } or proposal_hash is None
                or tx.snapshot.proposal_hash != proposal_hash
                or reason_code is not None):
            raise StateConflict("planned proposal has no current trusted admission")
        status = "PREPARED"
    else:
        if tx.snapshot.phase is not OfficePhase.ASSIGNED or proposal_hash is not None:
            raise StateConflict("failed planning must not alter task authority")
        if now >= lease.expires_at:
            result_kind, reason_code = "LEASE_EXPIRED", "LEASE_EXPIRED"
        elif result_kind not in ("NEEDS_CLARIFICATION", "RETRYABLE_FAILURE", "LEASE_EXPIRED"):
            raise StateConflict("invalid non-effect planning outcome")
        if reason_code is None:
            raise StateConflict("failed planning needs a bounded reason")
        status = ("EXHAUSTED" if row.attempts_used >= row.max_attempts
                  else "NEEDS_CLARIFICATION" if result_kind == "NEEDS_CLARIFICATION"
                  else "READY")
    _seal_outcome(tx, attempt, result_kind=result_kind,
                  reason_code=reason_code, proposal_hash=proposal_hash, now=now)
    row.status = status
    row.workspace_version += 1
    row.active_attempt_id = None
    row.lease_until = None
    row.last_reason = "ATTEMPT_EXHAUSTED" if status == "EXHAUSTED" else reason_code
    row.proposal_hash = proposal_hash
    row.updated_at = now
    tx.session.flush()


class PlanningWorkspaceStore:
    """Task-row-locked attempt/CAS store; no model output persisted at rest."""

    def __init__(
        self, repo: LedgerRepository, *, max_attempts: int = 3,
        lease_seconds: int = 90,
    ) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError("planning attempt budget must be 1..10")
        if not 5 <= lease_seconds <= 300:
            raise ValueError("planning lease must be 5..300 seconds")
        self._repo = repo
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds

    def view(self, principal: Principal, task_id: str, *, now: datetime,
             mode: PlanningMode) -> PlanningWorkspaceView:
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            principal.require("task:read", tx.task.organization_id, now)
            row = tx.session.get(PlanningWorkspaceRow, task_id)
            if row is None:
                status = "READY" if tx.snapshot.phase is OfficePhase.ASSIGNED else "PREPARED"
                return PlanningWorkspaceView(
                    task_id=task_id, workspace_version=0,
                    attempts_used=0, max_attempts=self.max_attempts,
                    status=status, mode=mode,
                )
            _assert_workspace(tx, row)
            if row.mode != mode:
                raise StateConflict("planner mode has changed since workspace creation")
            attempts = tx.session.scalars(
                select(PlanningAttemptRow).where(
                    PlanningAttemptRow.task_id == task_id,
                ).order_by(PlanningAttemptRow.attempt_number)
            ).all()
            if len(attempts) != row.attempts_used:
                raise StateConflict("planning attempt count integrity failure")
            missing = 0
            for i, attempt in enumerate(attempts, start=1):
                if (attempt.attempt_number != i
                        or attempt.manifest_hash != row.manifest_hash
                        or attempt.mode != row.mode):
                    raise StateConflict("planning attempt binding mismatch")
                outcome = tx.session.get(PlanningOutcomeRow, attempt.attempt_id)
                if outcome is None:
                    if attempt.attempt_id != row.active_attempt_id:
                        raise StateConflict("missing planning outcome")
                    missing += 1
                else:
                    payload = {
                        "attempt_id": attempt.attempt_id, "task_id": attempt.task_id,
                        "attempt_number": attempt.attempt_number,
                        "manifest_hash": attempt.manifest_hash,
                        "task_state_version": attempt.task_state_version,
                        "result_kind": outcome.result_kind,
                        "reason_code": outcome.reason_code,
                        "proposal_hash": outcome.proposal_hash,
                        "recorded_at": _utc(outcome.recorded_at),
                    }
                    if (outcome.task_id != task_id
                            or sha256(payload) != outcome.outcome_hash):
                        raise StateConflict("planning outcome integrity failure")
            if (missing != int(row.status == "RUNNING")
                    or (row.status == "RUNNING") != (row.active_attempt_id is not None)
                    or (row.status == "PREPARED"
                        and row.proposal_hash != tx.snapshot.proposal_hash)):
                raise StateConflict("planning workspace state integrity failure")
            return PlanningWorkspaceView(
                task_id=task_id, workspace_version=row.workspace_version,
                attempts_used=row.attempts_used, max_attempts=row.max_attempts,
                status=row.status, mode=mode,
                last_reason=row.last_reason, proposal_hash=row.proposal_hash,
            )

    def begin(
        self, principal: Principal, task_id: str, *,
        expected_task_version: int, expected_workspace_version: int,
        mode: PlanningMode, now: datetime,
    ) -> PlanningLease | None:
        now = require_aware(now).astimezone(UTC)
        with self._repo.write_task(task_id) as tx:
            task, snap = tx.task, tx.snapshot
            principal.require("task:prepare", task.organization_id, now)
            principal.require("task:propose", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("only task initiator may initiate a planning attempt")
            if (snap.phase is not OfficePhase.ASSIGNED
                    or snap.state_version != expected_task_version
                    or now >= task.deadline or snap.external_effects != 0
                    or snap.simulation_ref is not None or snap.proposal_hash is not None):
                raise StateConflict("task is not an eligible synthetic planning assignment")
            row = tx.session.get(PlanningWorkspaceRow, task_id)
            if row is None:
                if expected_workspace_version != 0:
                    raise StateConflict("planning workspace version mismatch")
                row = PlanningWorkspaceRow(
                    task_id=task_id, organization_id=task.organization_id,
                    initiator_id=task.initiator_id, manifest_hash=task.manifest_hash,
                    mode=mode, workspace_version=0, attempts_used=0,
                    max_attempts=self.max_attempts, status="READY",
                    active_attempt_id=None, lease_until=None, last_reason=None,
                    proposal_hash=None, updated_at=now,
                )
                tx.session.add(row)
                tx.session.flush()
            _assert_workspace(tx, row)
            if row.mode != mode or row.workspace_version != expected_workspace_version:
                raise StateConflict("planning workspace mode or version mismatch")
            if row.active_attempt_id is not None:
                if row.lease_until is None or now < _utc(row.lease_until):
                    raise StateConflict("planning attempt is already running")
                lease_attempt = tx.session.get(PlanningAttemptRow, row.active_attempt_id)
                if lease_attempt is None:
                    raise StateConflict("active planning attempt evidence missing")
                _seal_outcome(
                    tx, lease_attempt, result_kind="LEASE_EXPIRED",
                    reason_code="LEASE_EXPIRED", proposal_hash=None, now=now,
                )
                row.active_attempt_id = None
                row.lease_until = None
                row.workspace_version += 1
                row.last_reason = "LEASE_EXPIRED"
                row.status = "READY"
            if row.attempts_used >= row.max_attempts:
                row.status = "EXHAUSTED"
                row.last_reason = "ATTEMPT_EXHAUSTED"
                row.updated_at = now
                return None
            attempt_number = row.attempts_used + 1
            attempt_id = "plan_" + token_hex(16)
            expiry = min(now + timedelta(seconds=self.lease_seconds),
                         task.deadline.astimezone(UTC))
            if expiry <= now:
                raise StateConflict("task deadline no longer permits planning")
            row.active_attempt_id = attempt_id
            row.lease_until = expiry
            row.status = "RUNNING"
            row.workspace_version += 1
            row.attempts_used = attempt_number
            row.last_reason = None
            row.updated_at = now
            tx.session.add(PlanningAttemptRow(
                attempt_id=attempt_id, task_id=task_id,
                attempt_number=attempt_number,
                task_state_version=snap.state_version,
                manifest_hash=task.manifest_hash, mode=mode,
                started_at=now, lease_until=expiry,
            ))
            tx.session.flush()
            return PlanningLease(
                task_id=task_id, attempt_id=attempt_id,
                workspace_version=row.workspace_version,
                attempt_number=attempt_number, task_state_version=snap.state_version,
                manifest_hash=task.manifest_hash, mode=mode,
                expires_at=expiry,
            )

    def fail(
        self, lease: PlanningLease, *, result_kind: PlanningResultKind,
        reason_code: PlanningReason, now: datetime,
    ) -> None:
        if result_kind == "PROPOSED":
            raise StateConflict("only trusted atomic coordinator may admit proposal")
        with self._repo.write_task(lease.task_id) as tx:
            finish_planning_attempt(
                tx, lease, result_kind=result_kind,
                reason_code=reason_code, proposal_hash=None, now=now,
            )
