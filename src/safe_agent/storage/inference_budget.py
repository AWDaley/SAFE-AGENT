"""M7-03 transactional, conservative inference budget reservations.

Budget rows form one globally serialized admission lane (PostgreSQL row lock,
SQLite isolated test BEGIN IMMEDIATE), preventing parallel oversubscription.
No reported usage may release held funds without separately verified billing;
this milestone intentionally has NO auto-refund or live provider reconciler.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.inference import (
    InferenceReportedUsage,
    InferenceRequest,
    InferenceRoutePolicy,
)
from safe_agent.contracts.inference_budget import (
    InferenceBudgetLimits,
    InferenceBudgetReceipt,
    InferenceBudgetScopeView,
    UncertaintyReason,
)
from safe_agent.evidence.ledger import OfficePhase, TaskSnapshot
from safe_agent.storage.models import (
    InferenceBudgetEventRow,
    InferenceBudgetReservationRow,
    InferenceBudgetScopeRow,
    TaskRow,
)
from safe_agent.storage.repository import LedgerRepository


class InferenceBudgetDenied(ValueError):
    """Fixed safe refusal: do not expose pricing, task metadata or SQL errors."""

    def __init__(self) -> None:
        super().__init__("INFERENCE_SPENDING_NOT_AUTHORIZED")


def _utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _ceil(value: int, divisor: int = 1_000_000) -> int:
    return (value + divisor - 1) // divisor


def _scope_keys(organization_id: str, task_id: str) -> tuple[str, str, str]:
    return ("global", "org:" + organization_id, "task:" + task_id)


def _receipt(row: InferenceBudgetReservationRow) -> InferenceBudgetReceipt:
    return InferenceBudgetReceipt.model_validate({
        "request_id": row.request_id,
        "organization_id": row.organization_id,
        "task_id": row.task_id,
        "route_id": row.route_id,
        "status": row.status,
        "maximum_held_microusd": row.maximum_held_microusd,
        "reported_estimated_microusd": row.reported_estimated_microusd,
        "reported_input_tokens": row.reported_input_tokens,
        "reported_output_tokens": row.reported_output_tokens,
        "reported_source": row.reported_source,
        "uncertainty_reason": row.uncertainty_reason,
        "version": row.version,
    })


class InferenceBudgetLedger:
    """Trusted SQL ledger, with explicit bootstrap and immutable budget profile.

    Caller is trusted harness code ONLY; do not bind configure, reserve,
    finish, or read into public HTTP handlers or model tool registries.
    """

    def __init__(self, repo: LedgerRepository, limits: InferenceBudgetLimits) -> None:
        self._sessions = sessionmaker(bind=repo.engine, expire_on_commit=False)
        self._limits = limits
        self._hash = sha256(limits)
        self._engine = repo.engine

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._sessions.begin() as session:
            if self._engine.dialect.name == "sqlite":
                # SQLite DEFERRED read/then/write can lose race to another
                # connection. Acquire a database writer lock before any read.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session

    def _lock(self, session: Session, key: str) -> InferenceBudgetScopeRow:
        row = session.scalar(
            select(InferenceBudgetScopeRow)
            .where(InferenceBudgetScopeRow.scope_key == key)
            .with_for_update(),
        )
        if row is None or row.profile_hash != self._hash:
            raise InferenceBudgetDenied()
        if (row.held_microusd < 0 or row.held_microusd > row.max_microusd
                or row.calls_used < 0 or row.calls_used > row.max_calls
                or row.active_calls < 0 or row.active_calls > row.max_inflight):
            raise InferenceBudgetDenied()
        return row

    def _new_scope(
        self, session: Session, key: str, *, cap: int,
        calls: int, inflight: int, now: datetime,
    ) -> InferenceBudgetScopeRow:
        row = InferenceBudgetScopeRow(
            scope_key=key, profile_hash=self._hash,
            max_microusd=cap, held_microusd=0,
            max_calls=calls, calls_used=0,
            max_inflight=inflight, active_calls=0,
            requires_review=False, updated_at=now,
        )
        session.add(row)
        session.flush()
        return row

    def configure(self, *, now: datetime) -> None:
        """Trusted deployment must explicitly initialize a global spending cap.

        Reconfiguration with a different policy fails closed. Increases,
        resets and reconciliation need a separately reviewed admin migration.
        """
        now = require_aware(now).astimezone(UTC)
        with self._transaction() as session:
            existing = session.scalar(
                select(InferenceBudgetScopeRow)
                .where(InferenceBudgetScopeRow.scope_key == "global")
                .with_for_update(),
            )
            if existing is None:
                self._new_scope(
                    session, "global", cap=self._limits.global_max_microusd,
                    calls=self._limits.global_max_calls,
                    inflight=self._limits.global_max_inflight, now=now,
                )
            elif (existing.profile_hash != self._hash
                  or existing.max_microusd != self._limits.global_max_microusd
                  or existing.max_calls != self._limits.global_max_calls
                  or existing.max_inflight != self._limits.global_max_inflight):
                raise InferenceBudgetDenied()

    def _scope_rows(
        self, session: Session, org: str, task: str, *,
        create: bool, now: datetime,
    ) -> tuple[InferenceBudgetScopeRow, InferenceBudgetScopeRow,
               InferenceBudgetScopeRow]:
        global_row = self._lock(session, "global")
        key_org, key_task = "org:" + org, "task:" + task
        rows = [global_row]
        for key, cap, max_calls, concurrent in (
            (key_org, self._limits.tenant_max_microusd,
             self._limits.tenant_max_calls, self._limits.tenant_max_inflight),
            (key_task, self._limits.task_max_microusd,
             self._limits.task_max_calls, self._limits.task_max_inflight),
        ):
            record = session.scalar(
                select(InferenceBudgetScopeRow)
                .where(InferenceBudgetScopeRow.scope_key == key)
                .with_for_update(),
            )
            if record is None and create:
                record = self._new_scope(
                    session, key, cap=cap, calls=max_calls,
                    inflight=concurrent, now=now,
                )
            if (record is None or record.profile_hash != self._hash
                    or record.max_microusd != cap
                    or record.max_calls != max_calls
                    or record.max_inflight != concurrent):
                raise InferenceBudgetDenied()
            rows.append(self._lock(session, key))
        return rows[0], rows[1], rows[2]

    @staticmethod
    def _event(
        session: Session, row: InferenceBudgetReservationRow, *,
        now: datetime, kind: str,
    ) -> None:
        previous_hash = None
        if row.version > 1:
            prior = session.get(InferenceBudgetEventRow, (row.request_id, row.version - 1))
            if prior is None:
                raise InferenceBudgetDenied()
            previous_hash = prior.event_hash
        payload = {
            "request_id": row.request_id,
            "request_hash": row.request_hash,
            "profile_hash": row.profile_hash,
            "maximum_held_microusd": row.maximum_held_microusd,
            "state": row.status,
            "reason": row.uncertainty_reason,
            "reported_input_tokens": row.reported_input_tokens,
            "reported_output_tokens": row.reported_output_tokens,
            "reported_source": row.reported_source,
            "reported_estimated_microusd": row.reported_estimated_microusd,
            "proposal_digest": row.proposal_digest,
            "version": row.version,
        }
        digest = sha256({
            "previous_hash": previous_hash,
            "payload": payload,
            "recorded_at": now,
        })
        session.add(InferenceBudgetEventRow(
            request_id=row.request_id,
            ordinal=row.version,
            kind=kind,
            payload_json=payload,
            previous_hash=previous_hash,
            event_hash=digest,
            recorded_at=now,
        ))
        session.flush()

    def _terminal(
        self, session: Session, row: InferenceBudgetReservationRow, *,
        status: str, reason: UncertaintyReason | None,
        now: datetime,
        usage: InferenceReportedUsage | None = None,
        proposal_digest: str | None = None,
        freeze: bool = False,
    ) -> None:
        if row.status != "RESERVED":
            raise InferenceBudgetDenied()
        scopes = self._scope_rows(
            session, row.organization_id, row.task_id, create=False, now=now,
        )
        for scope in scopes:
            if scope.active_calls < 1:
                raise InferenceBudgetDenied()
            scope.active_calls -= 1
            scope.requires_review = scope.requires_review or freeze
            scope.updated_at = now
        row.status = status
        row.uncertainty_reason = reason
        if usage is not None:
            row.reported_input_tokens = usage.input_tokens
            row.reported_output_tokens = usage.output_tokens
            row.reported_source = usage.source
            row.reported_estimated_microusd = (
                _ceil(usage.input_tokens * row.input_price_microusd)
                + _ceil(usage.output_tokens * row.output_price_microusd)
            )
        row.proposal_digest = proposal_digest
        row.version += 1
        row.updated_at = now
        self._event(session, row, now=now, kind=status)

    def _expire_due(self, session: Session, *, now: datetime) -> None:
        """Recover expired in-flight slots, NEVER release reserved money."""
        due = session.scalars(
            select(InferenceBudgetReservationRow)
            .where(InferenceBudgetReservationRow.status == "RESERVED")
            .where(InferenceBudgetReservationRow.deadline_at <= now)
            .order_by(InferenceBudgetReservationRow.deadline_at)
            .limit(self._limits.global_max_inflight)
            .with_for_update(),
        ).all()
        for row in due:
            self._terminal(
                session, row, status="UNCERTAIN",
                reason="EXPIRED_UNRECONCILED", now=now,
            )

    def reserve(
        self, request: InferenceRequest, pricing: InferenceRoutePolicy, *,
        now: datetime,
    ) -> InferenceBudgetReceipt:
        now = require_aware(now).astimezone(UTC)
        try:
            checked = InferenceRequest.model_validate_json(request.model_dump_json())
            if (checked.route_id != pricing.route_id
                    or checked.model_revision != pricing.model_revision
                    or not _utc(checked.issued_at) <= now < _utc(checked.deadline_at)):
                raise InferenceBudgetDenied()
            prompt_bytes = len(canonical_json({
                "instruction": PLANNER_INSTRUCTION,
                "task": checked.task,
                "availability": checked.availability,
            }))
            if (prompt_bytes > pricing.max_prompt_bytes
                    or prompt_bytes > pricing.max_prompt_token_proxy):
                raise InferenceBudgetDenied()
            amount = (
                _ceil(prompt_bytes * pricing.input_microusd_per_million_tokens)
                + _ceil(pricing.max_response_token_proxy
                        * pricing.output_microusd_per_million_tokens)
            )
            if amount > pricing.max_estimated_cost_microusd:
                raise InferenceBudgetDenied()
            req_hash = sha256(checked)
        except (ValueError, TypeError, OverflowError):
            raise InferenceBudgetDenied() from None
        with self._transaction() as session:
            global_row = self._lock(session, "global")
            self._expire_due(session, now=now)
            if global_row.requires_review:
                raise InferenceBudgetDenied()
            task = session.get(TaskRow, checked.task.task_id)
            if (task is None or task.organization_id != checked.task.organization_id
                    or task.task_json.get("manifest_hash") != task.manifest_hash
                    or now >= _utc(task.deadline)):
                raise InferenceBudgetDenied()
            snap = TaskSnapshot.model_validate_json(json.dumps(task.snapshot_json))
            if snap.phase is not OfficePhase.ASSIGNED:
                raise InferenceBudgetDenied()
            if session.get(InferenceBudgetReservationRow, checked.request_id) is not None:
                # Never reuse an inference ID, even for byte-identical replay.
                raise InferenceBudgetDenied()
            scopes = self._scope_rows(
                session, checked.task.organization_id,
                checked.task.task_id, create=True, now=now,
            )
            for scope in scopes:
                if (scope.requires_review
                        or scope.held_microusd + amount > scope.max_microusd
                        or scope.calls_used >= scope.max_calls
                        or scope.active_calls >= scope.max_inflight):
                    raise InferenceBudgetDenied()
            for scope in scopes:
                scope.held_microusd += amount
                scope.calls_used += 1
                scope.active_calls += 1
                scope.updated_at = now
            row = InferenceBudgetReservationRow(
                request_id=checked.request_id,
                organization_id=checked.task.organization_id,
                task_id=checked.task.task_id,
                route_id=checked.route_id,
                model_revision=checked.model_revision,
                prompt_digest=checked.prompt_digest,
                request_hash=req_hash,
                profile_hash=self._hash,
                price_policy_hash=sha256(pricing),
                input_price_microusd=pricing.input_microusd_per_million_tokens,
                output_price_microusd=pricing.output_microusd_per_million_tokens,
                input_proxy_bytes=prompt_bytes,
                output_proxy_bytes=pricing.max_response_token_proxy,
                maximum_held_microusd=amount,
                status="RESERVED", uncertainty_reason=None,
                reported_input_tokens=None, reported_output_tokens=None,
                reported_source=None, reported_estimated_microusd=None,
                proposal_digest=None,
                issued_at=now, deadline_at=_utc(checked.deadline_at),
                updated_at=now, version=1,
            )
            session.add(row)
            session.flush()
            self._event(session, row, now=now, kind="RESERVED")
            return _receipt(row)

    def _read_matching(
        self, session: Session, request: InferenceRequest,
    ) -> InferenceBudgetReservationRow:
        row = session.scalar(
            select(InferenceBudgetReservationRow)
            .where(InferenceBudgetReservationRow.request_id == request.request_id)
            .with_for_update(),
        )
        if (row is None or row.request_hash != sha256(request)
                or row.profile_hash != self._hash
                or row.organization_id != request.task.organization_id
                or row.task_id != request.task.task_id
                or row.route_id != request.route_id
                or row.model_revision != request.model_revision
                or row.prompt_digest != request.prompt_digest):
            raise InferenceBudgetDenied()
        return row

    def finish(
        self, request: InferenceRequest, *,
        now: datetime, proposal_digest: str,
        usage: InferenceReportedUsage | None,
    ) -> InferenceBudgetReceipt:
        """Store unverified reported usage and KEEP the full estimated hold."""
        now = require_aware(now).astimezone(UTC)
        with self._transaction() as session:
            self._lock(session, "global")
            row = self._read_matching(session, request)
            if row.status == "RECORDED_UNVERIFIED":
                if (row.proposal_digest != proposal_digest
                        or ((usage is None) != (row.reported_source is None))
                        or (usage is not None and (
                            row.reported_input_tokens != usage.input_tokens
                            or row.reported_output_tokens != usage.output_tokens
                            or row.reported_source != usage.source
                        ))):
                    raise InferenceBudgetDenied()
                return _receipt(row)
            if row.status != "RESERVED":
                raise InferenceBudgetDenied()
            if now >= _utc(row.deadline_at):
                self._terminal(
                    session, row, status="UNCERTAIN",
                    reason="EXPIRED_UNRECONCILED", now=now,
                )
                return _receipt(row)
            if (usage is not None and (
                usage.input_tokens > row.input_proxy_bytes
                or usage.output_tokens > row.output_proxy_bytes
                or _ceil(usage.input_tokens * row.input_price_microusd)
                    + _ceil(usage.output_tokens * row.output_price_microusd)
                    > row.maximum_held_microusd
            )):
                self._terminal(
                    session, row, status="UNCERTAIN",
                    reason="REPORTED_USAGE_OVERRUN", now=now, freeze=True,
                )
                return _receipt(row)
            self._terminal(
                session, row, status="RECORDED_UNVERIFIED",
                reason=None, now=now, usage=usage,
                proposal_digest=proposal_digest,
            )
            return _receipt(row)

    def uncertain(
        self, request: InferenceRequest, *, reason: UncertaintyReason,
        now: datetime,
    ) -> InferenceBudgetReceipt:
        """Fail or lose response after possible charge: retain maximum hold."""
        now = require_aware(now).astimezone(UTC)
        with self._transaction() as session:
            self._lock(session, "global")
            row = self._read_matching(session, request)
            if row.status == "UNCERTAIN":
                return _receipt(row)
            if row.status != "RESERVED":
                raise InferenceBudgetDenied()
            self._terminal(
                session, row, status="UNCERTAIN",
                reason=reason, now=now,
                freeze=(reason == "REPORTED_USAGE_OVERRUN"),
            )
            return _receipt(row)

    def recover(self, *, now: datetime) -> int:
        """Trusted periodic recovery; expired calls stay financially reserved."""
        now = require_aware(now).astimezone(UTC)
        with self._transaction() as session:
            self._lock(session, "global")
            before = session.scalar(
                select(InferenceBudgetScopeRow.active_calls)
                .where(InferenceBudgetScopeRow.scope_key == "global"),
            )
            if before is None:
                raise InferenceBudgetDenied()
            self._expire_due(session, now=now)
            after = session.scalar(
                select(InferenceBudgetScopeRow.active_calls)
                .where(InferenceBudgetScopeRow.scope_key == "global"),
            )
            assert after is not None
            return before - after

    def receipt(self, request: InferenceRequest) -> InferenceBudgetReceipt:
        """Trusted read, verifying immutable hash-linked per-request evidence."""
        with self._sessions() as session:
            row = session.get(InferenceBudgetReservationRow, request.request_id)
            if row is None or row.request_hash != sha256(request):
                raise InferenceBudgetDenied()
            events = session.scalars(
                select(InferenceBudgetEventRow)
                .where(InferenceBudgetEventRow.request_id == request.request_id)
                .order_by(InferenceBudgetEventRow.ordinal),
            ).all()
            if len(events) != row.version or not events:
                raise InferenceBudgetDenied()
            previous: str | None = None
            for index, event in enumerate(events, 1):
                if (event.ordinal != index or event.previous_hash != previous
                        or event.event_hash != sha256({
                            "previous_hash": event.previous_hash,
                            "payload": event.payload_json,
                            "recorded_at": _utc(event.recorded_at),
                        })):
                    raise InferenceBudgetDenied()
                previous = event.event_hash
            if (events[-1].payload_json.get("state") != row.status
                    or events[-1].payload_json.get("maximum_held_microusd")
                    != row.maximum_held_microusd
                    or events[-1].payload_json.get("reported_input_tokens")
                    != row.reported_input_tokens
                    or events[-1].payload_json.get("reported_output_tokens")
                    != row.reported_output_tokens
                    or events[-1].payload_json.get("reported_source")
                    != row.reported_source
                    or events[-1].payload_json.get("reported_estimated_microusd")
                    != row.reported_estimated_microusd
                    or events[-1].payload_json.get("proposal_digest") != row.proposal_digest
                    or events[-1].payload_json.get("reason") != row.uncertainty_reason):
                raise InferenceBudgetDenied()
            return _receipt(row)

    def scope_view(self, key: str) -> InferenceBudgetScopeView:
        """Trusted administrative view only; not a public API or model tool."""
        with self._sessions() as session:
            row = session.get(InferenceBudgetScopeRow, key)
            if row is None or row.profile_hash != self._hash:
                raise InferenceBudgetDenied()
            return InferenceBudgetScopeView(
                scope_key=row.scope_key, profile_hash=row.profile_hash,
                max_microusd=row.max_microusd, held_microusd=row.held_microusd,
                max_calls=row.max_calls, calls_used=row.calls_used,
                max_inflight=row.max_inflight, active_calls=row.active_calls,
                requires_review=row.requires_review,
            )
