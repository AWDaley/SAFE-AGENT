"""M7-05b: atomic, non-dispatching inference admission for trusted callers.

One task lock, a shared admission lock and the canonical cost ledger's locks
cover all writes. No transport, provider credentials or network calls occur.
A repeated attempt returns metadata only, never a new execution permission.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.canonical import sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.contracts.inference import InferenceRequest
from safe_agent.contracts.inference_admission import InferenceAdmissionPolicy, InferenceAdmissionReceipt
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    InferenceInvocationEvent,
    InferenceInvocationView,
    bind_planning_attempt,
    replay_invocation,
    seal_invocation_event,
)
from safe_agent.contracts.office import Projection
from safe_agent.contracts.planning import PlanningLease
from safe_agent.evidence.ledger import LedgerEvent, OfficePhase, replay
from safe_agent.inference.gateway import _div_ceiling, _prompt_models
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_budget import InferenceBudgetLedger, _audit_events, _receipt
from safe_agent.storage.models import (
    InferenceAdmissionAuthorityRow,
    InferenceAdmissionRow,
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
    InferenceInvocationRow,
    LedgerRow,
    PlanningAttemptRow,
    PlanningOutcomeRow,
    PlanningWorkspaceRow,
)
from safe_agent.storage.planning import _assert_workspace, _utc
from safe_agent.storage.repository import LedgerRepository, TaskTransaction


class InferenceAdmissionDenied(ValueError):
    """Fixed public error: no tenant balances, model text, SQL or credentials."""

    def __init__(self) -> None:
        super().__init__("INFERENCE_ADMISSION_DENIED")


def _task_time_matches(stored: datetime, original: datetime) -> bool:
    # Legacy task rows (unlike planning/invocation rows) were written with the
    # source offset, which SQLite strips without converting the wall time.
    # PostgreSQL preserves the instant. Do not rewrite historical task data.
    if stored.tzinfo is None:
        return stored == original.replace(tzinfo=None)
    return stored == original


def _task_identity(tx: TaskTransaction, principal: Principal, lease: PlanningLease,
                   context: SafeTaskContext, now: datetime) -> PlanningAttemptRow:
    task, manifest, snapshot = tx.task, tx.manifest, tx.snapshot
    principal.require("task:prepare", task.organization_id, now)
    principal.require("task:propose", task.organization_id, now)
    if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
            or task.initiator_id != principal.actor_id
            or (task.task_id, task.organization_id, task.initiator_id, task.manifest_hash)
               != (tx.row.task_id, tx.row.organization_id, tx.row.initiator_id, tx.row.manifest_hash)
            or (snapshot.task_id, snapshot.organization_id, snapshot.initiator_id,
                snapshot.manifest_hash, snapshot.state_version, snapshot.ledger_sequence)
               != (task.task_id, task.organization_id, task.initiator_id, task.manifest_hash,
                   tx.row.state_version, tx.row.ledger_sequence)
            or task.manifest_id != tx.row.manifest_id or manifest.manifest_id != task.manifest_id
            or not _task_time_matches(tx.row.created_at, task.created_at)
            or not _task_time_matches(tx.row.deadline, task.deadline)
            or lease.task_id != task.task_id or lease.manifest_hash != task.manifest_hash
            or lease.mode != "text_only"
            or context.task_id != task.task_id or context.organization_id != task.organization_id
            or manifest.task_id != task.task_id or manifest.organization_id != task.organization_id
            or manifest.initiator_id != task.initiator_id
            or manifest.permitted_operations != ("calendar.create_event",)
            or context.calendar_ref != manifest.calendar_ref
            or context.customer_ref != manifest.customer_ref
            or context.template_id != manifest.template_id
            or context.time_zone != manifest.time_zone
            or context.allowed_start != manifest.allowed_start
            or context.allowed_end != manifest.allowed_end
            or context.duration_minutes > manifest.max_duration_minutes):
        raise InferenceAdmissionDenied()
    attempt = tx.session.get(PlanningAttemptRow, lease.attempt_id)
    if (attempt is None or attempt.task_id != task.task_id
            or attempt.manifest_hash != lease.manifest_hash or attempt.mode != lease.mode
            or attempt.attempt_number != lease.attempt_number
            or attempt.task_state_version != lease.task_state_version
            or _utc(attempt.lease_until) != lease.expires_at
            or now < _utc(attempt.started_at)):
        raise InferenceAdmissionDenied()
    # Validate task history inside the SAME transaction, not through a second session.
    rows = tx.session.scalars(select(LedgerRow).where(
        LedgerRow.task_id == task.task_id,
    ).order_by(LedgerRow.ledger_sequence)).all()
    events = tuple(LedgerEvent.model_validate_json(json.dumps(r.event_json)) for r in rows)
    for r, event in zip(rows, events, strict=True):
        if (r.event_hash != event.event_hash or r.previous_event_hash != event.previous_event_hash
                or r.ledger_sequence != event.ledger_sequence
                or r.organization_id != task.organization_id):
            raise InferenceAdmissionDenied()
    if replay(events) != snapshot:
        raise InferenceAdmissionDenied()
    return attempt


def _live_attempt(tx: TaskTransaction, lease: PlanningLease, now: datetime) -> None:
    row = tx.session.get(PlanningWorkspaceRow, lease.task_id)
    if row is None:
        raise InferenceAdmissionDenied()
    _assert_workspace(tx, row)
    if (row.active_attempt_id != lease.attempt_id or row.workspace_version != lease.workspace_version
            or row.status != "RUNNING" or row.mode != "text_only"
            or row.attempts_used != lease.attempt_number
            or row.lease_until is None or _utc(row.lease_until) != lease.expires_at
            or now < _utc(row.updated_at) or now >= lease.expires_at or now >= tx.task.deadline
            or tx.snapshot.phase is not OfficePhase.ASSIGNED
            or tx.snapshot.state_version != lease.task_state_version
            or tx.snapshot.proposal_hash is not None or tx.snapshot.simulation_ref is not None
            or tx.snapshot.external_effects != 0
            or tx.session.get(PlanningOutcomeRow, lease.attempt_id) is not None):
        raise InferenceAdmissionDenied()


def _binding(row: InferenceInvocationRow) -> InferenceInvocationBinding:
    values = {field: getattr(row, field) for field in InferenceInvocationBinding.model_fields
              if field != "schema_version"}
    for field in ("issued_at", "deadline_at", "lease_expires_at"):
        values[field] = _utc(values[field])
    bound = InferenceInvocationBinding.model_validate(values)
    if bound.binding_hash != row.binding_hash:
        raise InferenceAdmissionDenied()
    return bound


def _history(tx: TaskTransaction, row: InferenceInvocationRow,
             bound: InferenceInvocationBinding) -> InferenceInvocationView:
    rows = tx.session.scalars(select(InferenceInvocationEventRow).where(
        InferenceInvocationEventRow.request_id == row.request_id,
    ).order_by(InferenceInvocationEventRow.sequence).limit(4)).all()
    # The frozen v1 lifecycle permits at most three events, never an unbounded scan.
    if not 1 <= len(rows) <= 3:
        raise InferenceAdmissionDenied()
    events = []
    for event_row in rows:
        data = {field: getattr(event_row, field) for field in InferenceInvocationEvent.model_fields
                if field != "schema_version"}
        data["occurred_at"] = _utc(event_row.occurred_at)
        events.append(InferenceInvocationEvent.model_validate(data))
    view = replay_invocation(bound, events)
    if (view.state != row.state or view.state_version != row.state_version
            or view.event_sequence != row.event_sequence or view.latest_event_hash != row.latest_event_hash
            or view.reason_code != row.reason_code or view.proposal_hash != row.proposal_hash
            or _utc(row.updated_at) < events[-1].occurred_at):
        raise InferenceAdmissionDenied()
    dispatched = next((e.occurred_at for e in events if e.state == "DISPATCH_STARTED"), None)
    if dispatched != (_utc(row.dispatch_started_at) if row.dispatch_started_at else None):
        raise InferenceAdmissionDenied()
    return view


class TrustedInferenceAdmission:
    """Opt-in host API: admit one request per verified planning attempt.

    This is deliberately NOT a generate callback or a sidecar dispatcher. The
    legacy inference gateway remains an offline fixture; M7-05c/d must wire a
    protected dispatch coordinator and attempt-scoped planning adapter.
    """

    def __init__(self, repo: LedgerRepository, *, policy: InferenceAdmissionPolicy,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._repo = repo
        self._policy = InferenceAdmissionPolicy.model_validate(policy.model_dump())
        self._policy_hash = sha256(self._policy)
        # Accounting is mandatory even for zero-price offline admission.
        self._budget = InferenceBudgetLedger(repo, self._policy.budget)
        self._clock = clock

    def _authority(self, tx: TaskTransaction) -> InferenceAdmissionAuthorityRow:
        table = InferenceAdmissionAuthorityRow
        dialect = self._repo.engine.dialect.name
        if dialect not in ("postgresql", "sqlite"):
            raise InferenceAdmissionDenied()
        # Bootstrap is serialized as well: INSERT ON CONFLICT never replaces limits.
        stmt = pg_insert(table) if dialect == "postgresql" else sqlite_insert(table)
        at = require_aware(self._clock()).astimezone(UTC)
        tx.session.execute(stmt.values(
            authority_key="global", policy_hash=self._policy_hash,
            created_at=at, last_seen_at=at,
        ).on_conflict_do_nothing(index_elements=["authority_key"]))
        row = tx.session.scalar(select(table).where(
            table.authority_key == "global",
        ).with_for_update())
        if row is None or row.policy_hash != self._policy_hash:
            raise InferenceAdmissionDenied()
        return row

    def _capacity(self, tx: TaskTransaction, context: SafeTaskContext, now: datetime) -> None:
        lim = self._policy.throughput
        a, inv = InferenceAdmissionRow, InferenceInvocationRow
        scopes = (
            ((), lim.global_requests, lim.global_outstanding),
            ((inv.organization_id == context.organization_id,),
             lim.tenant_requests, lim.tenant_outstanding),
            ((inv.organization_id == context.organization_id, inv.task_id == context.task_id),
             lim.task_requests, lim.task_outstanding),
        )
        for predicates, max_requests, max_outstanding in scopes:
            query = select(func.count()).select_from(a).join(inv, a.request_id == inv.request_id)
            query = query.where(*predicates)
            recent = tx.session.scalar(query.where(a.admitted_at > now - timedelta(
                seconds=lim.window_seconds,
            )))
            outstanding = tx.session.scalar(query.where(inv.state.in_(
                ("RESERVED", "DISPATCH_STARTED", "UNKNOWN"),
            )))
            # Expired RESERVED/DISPATCH_STARTED and UNKNOWN continue to consume
            # capacity. No lease-expiry refund, eviction or in-memory fallback.
            if recent is None or outstanding is None or recent >= max_requests or (
                outstanding >= max_outstanding
            ):
                raise InferenceAdmissionDenied()

    def _request(self, context: SafeTaskContext, projection: Projection, *, request_id: str,
                 issued_at: datetime, deadline_at: datetime) -> InferenceRequest:
        from safe_agent.agent.json_planner import PLANNER_INSTRUCTION

        return InferenceRequest(
            request_id=request_id, route_id=self._policy.route.route_id,
            model_revision=self._policy.route.model_revision,
            prompt_digest=sha256({"instruction": PLANNER_INSTRUCTION,
                                  "task": context, "availability": projection}),
            issued_at=issued_at, deadline_at=deadline_at, task=context, availability=projection,
        )

    def admit(self, principal: Principal, lease: PlanningLease, prompt: str) -> InferenceAdmissionReceipt:
        """Durable admission or exact-repeat metadata; never calls a model.

        Parse before locking, but recheck clocks/authority after obtaining all
        admission locks. A failure at any point rolls back every new record and
        hold. An old attempt can be inspected through exact repeat even after
        expiry; its receipt does not extend its deadline or permit dispatch.
        """
        try:
            actor = Principal.model_validate(principal.model_dump())
            claim = PlanningLease.model_validate(lease.model_dump())
            pol = self._policy.route
            context, projection, data = _prompt_models(
                prompt, max_bytes=min(pol.max_prompt_bytes, pol.max_prompt_token_proxy),
            )
            max_output = min(pol.max_response_bytes, pol.max_response_token_proxy)
            upper = (_div_ceiling(len(data) * pol.input_microusd_per_million_tokens, 1_000_000)
                     + _div_ceiling(max_output * pol.output_microusd_per_million_tokens, 1_000_000))
            if upper > pol.max_estimated_cost_microusd:
                raise InferenceAdmissionDenied()
            with self._repo.write_task(claim.task_id, serialize_sqlite=True) as tx:
                # Do not allow an unauthenticated/foreign caller to bootstrap the profile.
                _task_identity(tx, actor, claim, context, require_aware(self._clock()))
                authority = self._authority(tx)
                now = require_aware(self._clock()).astimezone(UTC)
                _task_identity(tx, actor, claim, context, now)
                if now < _utc(authority.last_seen_at):
                    raise InferenceAdmissionDenied()
                authority.last_seen_at = now
                row = tx.session.scalar(select(InferenceInvocationRow).where(
                    InferenceInvocationRow.organization_id == context.organization_id,
                    InferenceInvocationRow.task_id == claim.task_id,
                    InferenceInvocationRow.attempt_id == claim.attempt_id,
                ))
                intent_hash = sha256({"policy_hash": self._policy_hash,
                                      "lease": claim, "prompt_hash": sha256(data.decode("utf-8")),
                                      "max_microusd": upper})
                if row is not None:
                    bound = _binding(row)
                    request = self._request(
                        context, projection, request_id=bound.request_id,
                        issued_at=bound.issued_at, deadline_at=bound.deadline_at,
                    )
                    if bind_planning_attempt(request, claim) != bound:
                        raise InferenceAdmissionDenied()
                    admission = tx.session.get(InferenceAdmissionRow, row.request_id)
                    reserve = tx.session.get(InferenceBudgetReservationRow, row.request_id)
                    if (admission is None or reserve is None
                            or admission.binding_hash != bound.binding_hash
                            or admission.policy_hash != self._policy_hash
                            or admission.authority_key != "global" or admission.intent_hash != intent_hash
                            or _utc(admission.admitted_at) != bound.issued_at
                            or reserve.organization_id != context.organization_id
                            or reserve.task_id != claim.task_id or reserve.backend_kind != "REMOTE"
                            or reserve.route_id != bound.route_id or reserve.model_revision != bound.model_revision
                            or reserve.prompt_digest != bound.prompt_digest
                            or reserve.max_microusd != upper or _utc(reserve.deadline_at) != bound.deadline_at
                            or _utc(reserve.reserved_at) != bound.issued_at):
                        raise InferenceAdmissionDenied()
                    _audit_events(tx, reserve)
                    return InferenceAdmissionReceipt(
                        disposition="EXISTING", binding=bound, invocation=_history(tx, row, bound),
                        reservation=_receipt(reserve),
                    )
                _live_attempt(tx, claim, now)
                self._capacity(tx, context, now)
                deadline = min(now + timedelta(seconds=pol.timeout_seconds),
                               projection.expires_at, claim.expires_at, tx.task.deadline,
                               actor.authenticated_until)
                request = self._request(context, projection, request_id="inf_" + token_hex(16),
                                        issued_at=now, deadline_at=deadline)
                bound = bind_planning_attempt(request, claim)
                receipt = self._budget.reserve_in_transaction(
                    tx, request, max_microusd=upper, synthetic_mock=False, now=now,
                )
                event = seal_invocation_event(bound, state="RESERVED", occurred_at=now)
                tx.session.add(InferenceInvocationRow(
                    **bound.model_dump(exclude={"schema_version"}), binding_hash=bound.binding_hash,
                    state="RESERVED", state_version=0, claim_version=0, claim_owner=None,
                    claim_until=None, dispatch_started_at=None, proposal_hash=None, reason_code=None,
                    event_sequence=1, latest_event_hash=event.event_hash, updated_at=now,
                ))
                tx.session.flush()
                tx.session.add(InferenceInvocationEventRow(**event.model_dump(exclude={"schema_version"})))
                tx.session.add(InferenceAdmissionRow(
                    request_id=bound.request_id, binding_hash=bound.binding_hash,
                    authority_key="global", policy_hash=self._policy_hash,
                    intent_hash=intent_hash, admitted_at=now,
                ))
                tx.session.flush()
                # Budget-row contention may outlast the deadline even though
                # admission locks were acquired in time. Roll back, never issue
                # a newly CREATED receipt with already-expired authority.
                finished = require_aware(self._clock()).astimezone(UTC)
                if not now <= finished < deadline:
                    raise InferenceAdmissionDenied()
                authority.last_seen_at = finished
                return InferenceAdmissionReceipt(
                    disposition="CREATED", binding=bound,
                    invocation=replay_invocation(bound, (event,)), reservation=receipt,
                )
        except Exception:
            # Neither SQL error text nor untrusted input is safe to surface.
            # The transaction context has already rolled back on every error.
            raise InferenceAdmissionDenied() from None
