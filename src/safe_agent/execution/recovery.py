"""Trusted synthetic outbox worker: durable dispatch, recovery and verification.

New coordinated calendar effects require the reviewed upstream TAL runtime.
This requirement is reconstructed from durable task provenance by every worker;
optional constructor configuration cannot downgrade a coordinated task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from secrets import token_hex

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter, FakeCalendarConflict
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter, FakeTaskConflict
from safe_agent.broker import BrokerScope, ReferenceBroker, ReferenceDenied, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import ExecutionOutcome, ExecutionReceipt, Task, TaskManifest
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.evidence.ledger import EventKind, OfficePhase, TaskSnapshot
from safe_agent.storage.coordinated_execution import coordinated_execution_input
from safe_agent.storage.models import GoogleTestOutboxBindingRow
from safe_agent.storage.provider import ProviderIntegrityError
from safe_agent.storage.repository import LedgerRepository, OutboxClaim, StateConflict
from safe_agent.tal.reviewed_runtime import require_reviewed_tal_runtime
from safe_agent.tal.trusted_dispatch import (
    TALOfficeDispatchDenied,
    TrustedTALOfficeDispatchCheck,
)
from safe_agent.virtual_office import VirtualOffice
from safe_agent.virtual_office.models import CalendarProjection, TaskListProjection


class FaultPoint(StrEnum):
    BEFORE_DISPATCH = "before_dispatch"
    AFTER_PROVIDER_COMMIT = "after_provider_commit"
    RESPONSE_LOST = "response_lost"
    RATE_LIMIT = "rate_limit"


class InjectedCrash(BaseException):
    """Test-only uncatchable process-exit simulation: in-flight lease survives."""


class KnownNoEffect(ValueError):
    """Provider definitively did not accept the attempted operation."""


@dataclass(frozen=True)
class DispatchContext:
    """Trusted, typed dispatch inputs reconstructed from durable task state."""

    task: Task | InternalTaskRecord
    manifest: TaskManifest | InternalTaskManifest
    proposal: CalendarCreateEvent | InternalTaskCreateProposal
    projection: CalendarProjection | TaskListProjection
    snap: TaskSnapshot
    recorded_at: datetime
    protected_references: tuple[dict[str, object], ...]


class SyntheticRecoveryWorker:
    """Trusted worker; never takes actions directly from an LLM response.

    Look up an existing provider-owned action key before considering a new write.
    Legacy fixture tasks may omit TAL. Coordinated tasks may not, even when a
    differently configured worker or supervisor claims the same durable outbox.
    """

    def __init__(
        self, repo: LedgerRepository, broker: ReferenceBroker,
        calendar: FakeCalendarAdapter, *, task_adapter: FakeInternalTaskAdapter | None = None,
        office: VirtualOffice | None = None, worker_id: str | None = None,
        max_attempts: int = 3, tal_dispatch: TrustedTALOfficeDispatchCheck | None = None,
    ) -> None:
        if type(calendar) is not FakeCalendarAdapter or (task_adapter is not None
                                                      and type(task_adapter) is not FakeInternalTaskAdapter):
            raise TypeError("M4 worker is restricted to exact synthetic adapters")
        if not 1 <= max_attempts <= 10:
            raise ValueError("invalid bounded recovery attempts")
        self._repo = repo
        self._broker = broker
        self._calendar = calendar
        self._tasks = task_adapter
        self._office = office
        self._id = worker_id or "worker_" + token_hex(10)
        self._max_attempts = max_attempts
        self._tal_dispatch = tal_dispatch
        # A separate TAL audit DB cannot be committed atomically with SAFE's
        # SQL ledger. Surface sync gaps, never rewrite a verified provider result.
        self._audit_sync_warnings: dict[str, str] = {}

    def reconcile_tal_audit(self, task_id: str, *, now: datetime) -> str | None:
        """Trusted, read-only catch-up. Never re-dispatches a provider effect."""
        if (type(self._tal_dispatch) is not TrustedTALOfficeDispatchCheck
                or not self._tal_dispatch.has_audit):
            return None
        try:
            result = self._tal_dispatch.reconcile_audit(self._repo, task_id, now=now)
        except Exception:
            self._audit_sync_warnings[task_id] = "TAL_AUDIT_SYNC_REQUIRED"
            return "TAL_AUDIT_SYNC_REQUIRED"
        self._audit_sync_warnings.pop(task_id, None)
        return result

    def audit_sync_warning(self, task_id: str) -> str | None:
        """Safe diagnostic for an operator; contains no provider identifiers."""
        return self._audit_sync_warnings.get(task_id)

    def process_one(
        self, *, task_id: str | None = None, now: datetime,
        fault: FaultPoint | None = None,
    ) -> ExecutionReceipt | None:
        """Claim at most one action. May raise InjectedCrash in test fault mode.

        A superseded lease raises StateConflict: the old worker must not overwrite
        its successor's receipt or escalation. The supervisor treats this as an
        infrastructure error and leaves recovery to the current durable owner.
        """
        now = require_aware(now)
        claim = self._repo.claim_action(worker_id=self._id, now=now, task_id=task_id)
        if claim is None:
            if task_id is not None:
                self.reconcile_tal_audit(task_id, now=now)
            return None
        if fault is FaultPoint.BEFORE_DISPATCH:
            raise InjectedCrash("crash after durable claim and before provider call")
        try:
            result = self._apply_claim(claim, now=now, fault=fault)
        except InjectedCrash:
            raise
        except KnownNoEffect:
            result = self._escalate(claim, now=now, reason="REVALIDATION_FAILED")
        except (FakeCalendarConflict, FakeTaskConflict, ProviderIntegrityError, StateConflict):
            # This also fences escalation: a lost claim propagates StateConflict
            # instead of recording the old worker's result against a new lease.
            result = self._escalate(claim, now=now, reason="SOURCE_CONFLICT_OR_INTEGRITY")
        except Exception:
            if claim.attempts >= self._max_attempts:
                result = self._escalate(claim, now=now, reason="RECONCILIATION_EXHAUSTED")
            else:
                self._repo.release_action_unknown(
                    claim, now=now, reason="EFFECT_OUTCOME_UNCERTAIN",
                    delay=timedelta(seconds=5),
                )
                result = ExecutionReceipt(
                    action_hash=claim.action_hash, manifest_hash=claim.manifest_hash,
                    outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
                )
        if claim.operation == "calendar.create_event":
            self.reconcile_tal_audit(claim.task_id, now=now)
        return result

    def _apply_claim(self, claim: OutboxClaim, *, now: datetime,
                     fault: FaultPoint | None) -> ExecutionReceipt:
        with self._repo.write_task(claim.task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            outbox = tx.require_outbox_claim(claim)
            if (claim.provider_kind != "synthetic"
                    or outbox.provider_kind != "synthetic"
                    or tx.session.get(GoogleTestOutboxBindingRow, claim.action_hash) is not None
                    or outbox.status != "LEASED"
                    or outbox.lease_owner != claim.worker_id
                    or snap.phase is not OfficePhase.EXECUTING
                    or snap.proposal_hash != claim.proposal_hash
                    or task.manifest_hash != claim.manifest_hash
                    or sha256(manifest) != claim.manifest_hash
                    or outbox.operation != claim.operation):
                raise StateConflict("durable action is not an admitted pending intent")
            proposal = tx.read_active_proposal()
            if proposal is None or sha256(proposal) != claim.proposal_hash:
                raise StateConflict("persisted action proposal is inconsistent")
            if claim.operation == "calendar.create_event":
                if not isinstance(manifest, TaskManifest) or not isinstance(
                    proposal, CalendarCreateEvent
                ):
                    raise StateConflict("wrong calendar domain")
                projection: CalendarProjection | TaskListProjection
                calendar_projection, calendar_simulation = tx.preview(proposal.simulation_ref)
                projection = calendar_projection
                if (calendar_simulation.conflicts
                        or calendar_projection.source_revision != claim.source_revision
                        or proposal.projection_rev != calendar_projection.revision
                        or calendar_simulation.change_diff.after.start_at != proposal.start_at
                        or calendar_simulation.change_diff.after.end_at != proposal.end_at):
                    raise StateConflict("calendar intent/preview mismatch")
                coordinated = coordinated_execution_input(tx, proposal, calendar_projection)
                if coordinated is not None and (
                    outbox.source_hash != coordinated.source_fingerprint
                    or claim.source_hash != coordinated.source_fingerprint
                ):
                    raise StateConflict("COORDINATED_OUTBOX_SOURCE_DENIED")
            elif claim.operation == "tasks.create":
                if not isinstance(manifest, InternalTaskManifest) or not isinstance(
                    proposal, InternalTaskCreateProposal
                ) or self._tasks is None:
                    raise StateConflict("wrong internal task domain")
                task_projection, task_simulation = tx.task_preview(proposal.simulation_ref)
                projection = task_projection
                if (task_simulation.conflicts
                        or task_projection.source_revision != claim.source_revision
                        or proposal.projection_rev != task_projection.revision
                        or task_simulation.change_diff.after.task_code != proposal.task_code
                        or task_simulation.change_diff.after.due_at != proposal.due_at):
                    raise StateConflict("task intent/preview mismatch")
            else:
                raise StateConflict("unrecognized outbox operation")
            # Readback needs the original scoped identity even if it has since
            # been revoked. It must not reactivate that identity for new writes.
            protected = tx.protected_references()
            # Recorded action time is for EXISTING-EFFECT READBACK ONLY.
            context = DispatchContext(
                task=task, manifest=manifest, proposal=proposal,
                projection=projection, snap=snap, recorded_at=outbox.created_at,
                protected_references=protected,
            )
        if claim.operation == "calendar.create_event":
            assert isinstance(manifest, TaskManifest)
            assert isinstance(proposal, CalendarCreateEvent)
            return self._calendar_effect(claim, context, now=now, fault=fault)
        assert isinstance(manifest, InternalTaskManifest)
        assert isinstance(proposal, InternalTaskCreateProposal)
        return self._task_effect(claim, context, now=now, fault=fault)

    def _scope(self, claim: OutboxClaim, context: DispatchContext, *, now: datetime,
               committed: bool) -> BrokerScope:
        task = context.task
        scope_time = context.recorded_at if committed else now
        if scope_time.utcoffset() is None:
            scope_time = scope_time.replace(tzinfo=UTC)
        return BrokerScope(
            organization_id=claim.organization_id, initiator_id=task.initiator_id,
            task_id=claim.task_id, operation=claim.operation,  # type: ignore[arg-type]
            now=scope_time,
        )

    def _allow_new_dispatch(self, claim: OutboxClaim, context: DispatchContext,
                            *, now: datetime) -> None:
        """Commit final admission atomically with respect to supported revocation.

        The LEASED -> DISPATCHING commit is the ordering point. Revocation that
        wins this task lock prevents a new write. A later revocation cannot undo
        the admitted attempt, but every recovery attempt must revalidate anew.
        No provider I/O occurs while this SQL transaction holds its locks.
        """
        task, manifest, projection, snap = (
            context.task, context.manifest, context.projection, context.snap,
        )
        with self._repo.write_task(claim.task_id, serialize_sqlite=True) as tx:
            outbox = tx.require_outbox_claim(claim, now=now)
            if (claim.provider_kind != "synthetic"
                    or tx.session.get(GoogleTestOutboxBindingRow, claim.action_hash) is not None
                    or tx.snapshot != snap or snap.phase is not OfficePhase.EXECUTING
                    or snap.proposal_hash != claim.proposal_hash
                    or tx.task != task or tx.manifest != manifest):
                raise KnownNoEffect("dispatch task or outbox state changed")
            if (now >= task.deadline or now >= projection.expires_at or now < projection.fetched_at
                    or snap.external_effects >= manifest.budget.max_external_effects
                    or snap.cycles_used > manifest.budget.max_cycles):
                raise KnownNoEffect("expired task, projection or execution budget")
            approval = tx.read_approval(snap.approval_id) if snap.approval_id is not None else None
            if manifest.requires_approval:
                if snap.approval_id is None:
                    raise KnownNoEffect("required approval missing")
                if (approval is None or not approval.approved or approval.task_id != claim.task_id
                        or approval.manifest_hash != claim.manifest_hash
                        or approval.proposal_hash != claim.proposal_hash
                        or now >= approval.expires_at):
                    raise KnownNoEffect("approval expired or mismatched")
            # This fresh, locked read is essential: the initial readback snapshot
            # and an already populated broker are not current write authority.
            protected = tx.protected_references(require_active=True)
            required = {manifest.customer_ref}
            if isinstance(manifest, TaskManifest):
                required.add(manifest.calendar_ref)
            if not required <= {str(entry["handle"]) for entry in protected}:
                raise KnownNoEffect("current reference binding missing")
            try:
                self._broker.trusted_import(protected)
                scope = self._scope(claim, context, now=now, committed=False)
                self._broker.resolve(manifest.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER)
                if isinstance(manifest, TaskManifest):
                    self._broker.resolve(manifest.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR)
            except ReferenceDenied as error:
                raise KnownNoEffect("current reference scope denied") from error
            if claim.operation == "calendar.create_event":
                assert isinstance(task, Task) and isinstance(manifest, TaskManifest)
                assert isinstance(context.proposal, CalendarCreateEvent)
                calendar_projection, simulation = tx.preview(context.proposal.simulation_ref)
                coordinated = coordinated_execution_input(tx, context.proposal, calendar_projection)
                if coordinated is not None and outbox.source_hash != coordinated.source_fingerprint:
                    raise KnownNoEffect("COORDINATED_OUTBOX_SOURCE_DENIED")
                checker = self._tal_dispatch
                # Durable provenance makes TAL mandatory, regardless of bootstrap.
                if coordinated is not None and type(checker) is not TrustedTALOfficeDispatchCheck:
                    raise KnownNoEffect("COORDINATED_TAL_REQUIRED")
                if checker is not None:
                    if type(checker) is not TrustedTALOfficeDispatchCheck:
                        raise KnownNoEffect("TAL_CHECKER_TYPE_DENIED")
                    try:
                        if coordinated is not None:
                            require_reviewed_tal_runtime()
                        TrustedTALOfficeDispatchCheck.require(
                            checker, action_hash=claim.action_hash,
                            task=task, manifest=manifest, proposal=context.proposal,
                            projection=calendar_projection, simulation=simulation,
                            snapshot=snap, approval=approval, now=now,
                        )
                    except TALOfficeDispatchDenied as error:
                        raise KnownNoEffect("TAL_OFFICE_RUNTIME_DENIED") from error
            outbox.status = "DISPATCHING"
            tx.session.flush()

    def _calendar_effect(self, claim: OutboxClaim, context: DispatchContext,
                         *, now: datetime, fault: FaultPoint | None) -> ExecutionReceipt:
        manifest, proposal = context.manifest, context.proposal
        assert isinstance(manifest, TaskManifest)
        assert isinstance(proposal, CalendarCreateEvent)
        scope = self._scope(claim, context, now=now, committed=True)
        calendar_id = self._broker.trusted_resolve_readback(
            manifest.calendar_ref, protected_records=context.protected_references,
            scope=scope, kind=ResourceKind.CALENDAR,
        )
        customer_id = self._broker.trusted_resolve_readback(
            manifest.customer_ref, protected_records=context.protected_references,
            scope=scope, kind=ResourceKind.CUSTOMER,
        )
        request_hash = sha256({
            "calendar_id": calendar_id, "organization_id": claim.organization_id,
            "customer_id": customer_id, "start_at": proposal.start_at,
            "end_at": proposal.end_at, "template_id": proposal.template_id,
            "expected_revision": claim.source_revision,
            "expected_source_hash": claim.source_hash,
        })
        found = self._calendar.find_effect(
            organization_id=claim.organization_id, action_hash=claim.action_hash,
            request_hash=request_hash,
        )
        if found is None:
            if not self._calendar.supports_idempotency:
                raise KnownNoEffect("cannot redispatch without provider idempotency")
            source = self._calendar.reader().snapshot(
                calendar_id=calendar_id, organization_id=claim.organization_id,
            )
            if (source.calendar.source_revision != claim.source_revision
                    or source.source_hash != claim.source_hash):
                raise KnownNoEffect("provider source changed before dispatch")
            self._allow_new_dispatch(claim, context, now=now)
            if fault is FaultPoint.RATE_LIMIT:
                self._repo.release_action_unknown(
                    claim, now=now, reason="SYNTHETIC_RATE_LIMIT",
                    delay=timedelta(seconds=15),
                )
                return ExecutionReceipt(action_hash=claim.action_hash,
                                        manifest_hash=claim.manifest_hash,
                                        outcome=ExecutionOutcome.OUTCOME_UNKNOWN)
            found = self._calendar.trusted_writer().create(
                calendar_id=calendar_id, organization_id=claim.organization_id,
                customer_id=customer_id, start_at=proposal.start_at, end_at=proposal.end_at,
                template_id=proposal.template_id, expected_revision=claim.source_revision,
                expected_source_hash=claim.source_hash, action_hash=claim.action_hash,
            )
            if fault is FaultPoint.AFTER_PROVIDER_COMMIT:
                raise InjectedCrash("crash after provider commit and before receipt")
            if fault is FaultPoint.RESPONSE_LOST:
                raise ConnectionError("synthetic provider returned no response")
        observed = self._calendar.reader().booking(
            calendar_id=calendar_id, organization_id=claim.organization_id, event_ref=found,
        )
        if (observed is None or observed.booking_id != found
                or observed.customer_id != customer_id
                or observed.start_at != proposal.start_at
                or observed.end_at != proposal.end_at):
            raise ConnectionError("readback is unavailable or disagrees")
        latest = self._calendar.reader().snapshot(
            calendar_id=calendar_id, organization_id=claim.organization_id,
        )
        if latest.calendar.source_revision == claim.source_revision:
            raise ConnectionError("postcondition source version unchanged")
        verification_hash = sha256({
            "action_hash": claim.action_hash, "effect_ref": found,
            "observed": observed, "source_revision": latest.calendar.source_revision,
        })
        return self._verify(claim, found, verification_hash, now=now)

    def _task_effect(self, claim: OutboxClaim, context: DispatchContext,
                     *, now: datetime, fault: FaultPoint | None) -> ExecutionReceipt:
        assert self._tasks is not None
        manifest, proposal = context.manifest, context.proposal
        assert isinstance(manifest, InternalTaskManifest)
        assert isinstance(proposal, InternalTaskCreateProposal)
        customer_id = self._broker.trusted_resolve_readback(
            manifest.customer_ref, protected_records=context.protected_references,
            kind=ResourceKind.CUSTOMER,
            scope=self._scope(claim, context, now=now, committed=True),
        )
        request_hash = sha256({
            "organization_id": claim.organization_id, "customer_id": customer_id,
            "task_code": proposal.task_code, "due_at": proposal.due_at,
            "expected_revision": claim.source_revision,
            "expected_source_hash": claim.source_hash,
        })
        found = self._tasks.find_effect(
            organization_id=claim.organization_id, action_hash=claim.action_hash,
            request_hash=request_hash,
        )
        if found is None:
            if not self._tasks.supports_idempotency:
                raise KnownNoEffect("task provider lacks idempotent redispatch")
            current = self._tasks.reader().snapshot(organization_id=claim.organization_id)
            if (current.revision != claim.source_revision
                    or current.source_hash != claim.source_hash):
                raise KnownNoEffect("task-list revision changed")
            self._allow_new_dispatch(claim, context, now=now)
            if fault is FaultPoint.RATE_LIMIT:
                self._repo.release_action_unknown(
                    claim, now=now, reason="SYNTHETIC_RATE_LIMIT", delay=timedelta(seconds=15),
                )
                return ExecutionReceipt(action_hash=claim.action_hash,
                                        manifest_hash=claim.manifest_hash,
                                        outcome=ExecutionOutcome.OUTCOME_UNKNOWN)
            found = self._tasks.trusted_writer().create(
                organization_id=claim.organization_id, customer_id=customer_id,
                task_code=proposal.task_code, due_at=proposal.due_at, now=now,
                action_hash=claim.action_hash, expected_revision=claim.source_revision,
                expected_source_hash=claim.source_hash,
            )
            if fault is FaultPoint.AFTER_PROVIDER_COMMIT:
                raise InjectedCrash("crash after task commit")
            if fault is FaultPoint.RESPONSE_LOST:
                raise ConnectionError("task provider response lost")
        observed = self._tasks.reader().task(
            organization_id=claim.organization_id, task_ref=found,
        )
        if (observed is None or observed.task_id != found
                or observed.customer_id != customer_id
                or observed.task_code != proposal.task_code
                or observed.due_at != proposal.due_at or observed.status != "open"):
            raise ConnectionError("task postcondition not independently observed")
        fresh = self._tasks.reader().snapshot(organization_id=claim.organization_id)
        if fresh.revision == claim.source_revision:
            raise ConnectionError("task source revision did not advance")
        verification_hash = sha256({
            "action_hash": claim.action_hash, "effect_ref": found,
            "observed": observed, "source_revision": fresh.revision,
        })
        return self._verify(claim, found, verification_hash, now=now)

    def _verify(self, claim: OutboxClaim, effect_ref: str, verification_hash: str,
                *, now: datetime) -> ExecutionReceipt:
        verified = ExecutionReceipt(
            action_hash=claim.action_hash, manifest_hash=claim.manifest_hash,
            outcome=ExecutionOutcome.COMMITTED, effect_ref=effect_ref,
            verification_ref="verification_" + verification_hash[7:39],
        )
        with self._repo.write_task(claim.task_id, serialize_sqlite=True) as tx:
            tx.require_outbox_claim(claim, statuses=("LEASED", "DISPATCHING"))
            if tx.snapshot.phase is not OfficePhase.EXECUTING:
                raise StateConflict("not a pending execution")
            tx.write_execution_outcome(
                claim.action_hash, outcome="VERIFIED",
                receipt_json=verified.model_dump(mode="json"),
            )
            tx.append(EventKind.ACTION_VERIFIED, payload={
                "proposal_hash": claim.proposal_hash, "effect_ref": effect_ref,
                "verification_hash": verification_hash,
            }, actor="trusted_synthetic_verifier", now=now)
        if self._office is not None:
            source = (self._calendar.synthetic_state() if claim.operation == "calendar.create_event"
                      else self._tasks.synthetic_state() if self._tasks is not None else None)
            if source is not None:
                self._office.replace_synthetic_source(source)
        return verified

    def _escalate(self, claim: OutboxClaim, *, now: datetime, reason: str) -> ExecutionReceipt:
        receipt = ExecutionReceipt(
            action_hash=claim.action_hash, manifest_hash=claim.manifest_hash,
            outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
        )
        with self._repo.write_task(claim.task_id, serialize_sqlite=True) as tx:
            tx.require_outbox_claim(claim, statuses=("LEASED", "DISPATCHING"))
            if tx.snapshot.phase is not OfficePhase.EXECUTING:
                raise StateConflict("unknown action cannot be escalated")
            tx.write_execution_outcome(
                claim.action_hash, outcome="OUTCOME_UNKNOWN",
                receipt_json=receipt.model_dump(mode="json"),
            )
            tx.mark_outbox_terminal(
                claim.action_hash, status="NEEDS_REVIEW", reason=reason,
                owner=claim.worker_id,
            )
            tx.append(EventKind.ACTION_UNVERIFIED, payload={
                "proposal_hash": claim.proposal_hash,
                "reason_code": reason,
            }, actor="trusted_synthetic_reconciler", now=now)
        return receipt
