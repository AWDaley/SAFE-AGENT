"""Synthetic effect gateway: one admitted proposal, one durably recorded intent.

Coordinated calendars use their immutable W4 input as the source baseline and
must be queued to the TAL-enforcing worker. Legacy fixture execution remains
separate; no real-provider adapter is accepted.
"""

from __future__ import annotations

from datetime import datetime

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import (
    DecisionStatus,
    ExecutionOutcome,
    ExecutionReceipt,
    TaskManifest,
)
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.coordinated_execution import coordinated_execution_input
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import VirtualOffice


class SyntheticExecutionGateway:
    """Trusted synthetic adapter is the sole M3 business-state mutation point."""

    def __init__(
        self, repo: LedgerRepository, office: VirtualOffice,
        gate: OfficeGate, broker: ReferenceBroker,
        adapter: FakeCalendarAdapter,
        *, task_adapter: FakeInternalTaskAdapter | None = None,
    ) -> None:
        if type(adapter) is not FakeCalendarAdapter:
            raise TypeError("only the exact synthetic fake calendar adapter is supported")
        self._repo = repo
        self._office = office
        self._gate = gate
        self._broker = broker
        self._adapter = adapter
        self._reader = adapter.reader()
        self._writer = adapter.trusted_writer()
        if task_adapter is not None and type(task_adapter) is not FakeInternalTaskAdapter:
            raise TypeError("only the exact synthetic fake task adapter is supported")
        self._task_adapter = task_adapter

    def execute(
        self, principal: Principal, task_id: str, *,
        now: datetime, expected_state_version: int,
    ) -> ExecutionReceipt:
        """Legacy synchronous fixture API; coordinated tasks require enqueue()."""
        result = self._execute_impl(
            principal, task_id, now=now,
            expected_state_version=expected_state_version, enqueue_only=False,
        )
        assert isinstance(result, ExecutionReceipt)
        return result

    def enqueue(
        self, principal: Principal, task_id: str, *,
        now: datetime, expected_state_version: int,
    ) -> str:
        """Authorize and persist a durable outbox command without dispatching it."""
        if not self._adapter.supports_idempotency and not (
            self._task_adapter is not None and self._task_adapter.supports_idempotency
        ):
            raise StateConflict("M4 enqueue requires durable native-idempotent fake provider")
        result = self._execute_impl(
            principal, task_id, now=now,
            expected_state_version=expected_state_version, enqueue_only=True,
        )
        assert isinstance(result, str)
        return result

    def _execute_impl(
        self, principal: Principal, task_id: str, *,
        now: datetime, expected_state_version: int, enqueue_only: bool,
    ) -> ExecutionReceipt | str:
        """Validate and persist intent before dispatch; never retry blindly."""
        now = require_aware(now)
        if isinstance(self._repo.read_task_record(task_id), InternalTaskRecord):
            return self._execute_internal(principal, task_id, now=now,
                                          expected_state_version=expected_state_version,
                                          enqueue_only=enqueue_only)
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            if not isinstance(manifest, TaskManifest):
                raise StateConflict("not a synthetic calendar manifest")
            principal.require("task:execute", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("only the initiating actor may execute synthetic tasks")
            if expected_state_version != snap.state_version:
                raise StateConflict("stale execution state")
            if snap.phase is not OfficePhase.READY_TO_COMMIT or snap.proposal_hash is None:
                raise StateConflict("task is not eligible for synthetic execution")
            if (snap.external_effects >= manifest.budget.max_external_effects
                    or snap.cycles_used > manifest.budget.max_cycles
                    or now >= task.deadline):
                raise StateConflict("execution budget or deadline exceeded")
            proposal = tx.read_active_proposal()
            if (not isinstance(proposal, CalendarCreateEvent)
                    or sha256(proposal) != snap.proposal_hash):
                raise StateConflict("persisted calendar proposal is absent or invalid")
            if manifest.requires_approval:
                if snap.approval_id is None:
                    raise StateConflict("approval required for synthetic execution")
                approval = tx.read_approval(snap.approval_id)
                if (approval is None or not approval.approved
                        or approval.task_id != task.task_id
                        or approval.manifest_hash != task.manifest_hash
                        or approval.proposal_hash != snap.proposal_hash
                        or now >= approval.expires_at):
                    raise StateConflict("approval no longer authorizes active proposal")
            projection, simulation = tx.preview(proposal.simulation_ref)
            coordinated = coordinated_execution_input(tx, proposal, projection)
            if coordinated is not None and (not enqueue_only or not self._adapter.supports_idempotency):
                raise StateConflict("COORDINATED_TAL_WORKER_REQUIRED")
            trial = snap.model_copy(update={
                "phase": OfficePhase.SIMULATED,
                "cycles_used": snap.cycles_used - 1,
            })
            if self._gate.evaluate(
                task=task, manifest=manifest, snapshot=trial,
                proposal=proposal, projection=projection, simulation=simulation, now=now,
            ).decision not in {DecisionStatus.AUTHORIZED, DecisionStatus.APPROVAL_REQUIRED}:
                raise StateConflict("synthetic gate revalidation denied")
            recomputed = self._office.simulate(
                projection, simulation.change_diff.after,
                actor=task.initiator_id, customer_ref=manifest.customer_ref, now=now,
            )
            if recomputed != simulation or recomputed.conflicts:
                raise StateConflict("synthetic preview no longer matches source")
            scope = BrokerScope(
                organization_id=task.organization_id, initiator_id=task.initiator_id,
                task_id=task.task_id, operation="calendar.create_event", now=now,
            )
            calendar_id = self._broker.resolve(
                manifest.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR,
            )
            customer_id = self._broker.resolve(
                manifest.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER,
            )
            current = self._reader.snapshot(
                calendar_id=calendar_id, organization_id=task.organization_id,
            )
            cached_hash = self._office.trusted_projection_fingerprint(projection, now=now)
            expected_hash = coordinated.source_fingerprint if coordinated is not None else cached_hash
            if (cached_hash != expected_hash
                    or current.calendar.source_revision != projection.source_revision
                    or current.source_hash != expected_hash
                    or current.calendar.time_zone != manifest.time_zone):
                raise StateConflict("authoritative synthetic source has changed")
            action_hash = sha256({
                "task_id": task_id, "manifest_hash": task.manifest_hash,
                "proposal_hash": snap.proposal_hash, "cycle": snap.cycles_used,
            })
            tx.store_protected_references(self._broker.trusted_export(task_id=task_id))
            tx.write_execution_intent(
                action_hash, snap.proposal_hash, now=now,
                operation="calendar.create_event", source_revision=projection.source_revision,
                source_hash=expected_hash,
            )
            tx.append(
                EventKind.ACTION_INTENT_RECORDED,
                payload={"proposal_hash": snap.proposal_hash, "action_hash": action_hash},
                actor="trusted_synthetic_gateway", now=now,
            )
            proposal_hash = snap.proposal_hash

        if enqueue_only:
            if not self._adapter.supports_idempotency:
                raise StateConflict("M4 calendar enqueue requires durable fake provider")
            return action_hash
        if self._adapter.supports_idempotency:
            from safe_agent.execution.recovery import SyntheticRecoveryWorker
            receipt = SyntheticRecoveryWorker(
                self._repo, self._broker, self._adapter,
                task_adapter=self._task_adapter, office=self._office,
            ).process_one(task_id=task_id, now=now)
            if receipt is None:
                raise StateConflict("enqueued calendar action was not claimed")
            return receipt

        effect_ref: str | None = None
        verification_hash: str | None = None
        try:
            effect_ref = self._writer.create(
                calendar_id=calendar_id, organization_id=task.organization_id,
                customer_id=customer_id, start_at=proposal.start_at,
                end_at=proposal.end_at, template_id=proposal.template_id,
                expected_revision=projection.source_revision,
                expected_source_hash=expected_hash, action_hash=action_hash,
            )
            observed = self._reader.booking(
                calendar_id=calendar_id, organization_id=task.organization_id,
                event_ref=effect_ref,
            )
            if (observed is None or observed.booking_id != effect_ref
                    or observed.customer_id != customer_id
                    or observed.start_at != proposal.start_at
                    or observed.end_at != proposal.end_at):
                raise ValueError("independent fake-provider postcondition failed")
            fresh = self._reader.snapshot(
                calendar_id=calendar_id, organization_id=task.organization_id,
            )
            if fresh.calendar.source_revision == projection.source_revision:
                raise ValueError("provider revision did not advance after effect")
            verification_hash = sha256({
                "action_hash": action_hash,
                "effect_ref": effect_ref,
                "observed": observed,
                "source_revision": fresh.calendar.source_revision,
            })
        except Exception:
            unknown = ExecutionReceipt(
                action_hash=action_hash, manifest_hash=task.manifest_hash,
                outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
                effect_ref=effect_ref,
            )
            with self._repo.write_task(task_id) as tx:
                if (tx.snapshot.phase is not OfficePhase.EXECUTING
                        or tx.snapshot.proposal_hash != proposal_hash):
                    raise StateConflict("execution intent no longer owns this task") from None
                tx.write_execution_outcome(action_hash, outcome="OUTCOME_UNKNOWN",
                                           receipt_json=unknown.model_dump(mode="json"))
                tx.append(
                    EventKind.ACTION_UNVERIFIED,
                    payload={"proposal_hash": proposal_hash,
                             "reason_code": "FAKE_EFFECT_REQUIRES_RECONCILIATION"},
                    actor="trusted_synthetic_gateway", now=now,
                )
            return unknown

        assert effect_ref is not None and verification_hash is not None
        verified = ExecutionReceipt(
            action_hash=action_hash, manifest_hash=task.manifest_hash,
            outcome=ExecutionOutcome.COMMITTED, effect_ref=effect_ref,
            verification_ref="verification_" + verification_hash[7:39],
        )
        with self._repo.write_task(task_id) as tx:
            if (tx.snapshot.phase is not OfficePhase.EXECUTING
                    or tx.snapshot.proposal_hash != proposal_hash):
                raise StateConflict("execution intent no longer owns this task")
            tx.write_execution_outcome(action_hash, outcome="VERIFIED",
                                       receipt_json=verified.model_dump(mode="json"))
            tx.append(
                EventKind.ACTION_VERIFIED,
                payload={"proposal_hash": proposal_hash, "effect_ref": effect_ref,
                         "verification_hash": verification_hash},
                actor="trusted_synthetic_verifier", now=now,
            )
        self._office.replace_synthetic_source(self._adapter.synthetic_state())
        return verified

    def _execute_internal(
        self, principal: Principal, task_id: str, *, now: datetime,
        expected_state_version: int, enqueue_only: bool,
    ) -> ExecutionReceipt | str:
        """Same trusted gateway, durable intent and replay for tasks.create."""
        if self._task_adapter is None:
            raise StateConflict("synthetic internal task adapter is not configured")
        reader = self._task_adapter.reader()
        writer = self._task_adapter.trusted_writer()
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            if not isinstance(task, InternalTaskRecord) or not isinstance(manifest, InternalTaskManifest):
                raise StateConflict("invalid internal task contract")
            principal.require("task:execute", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("only the initiating actor may execute synthetic tasks")
            if expected_state_version != snap.state_version:
                raise StateConflict("stale internal task state")
            if (snap.phase is not OfficePhase.READY_TO_COMMIT
                    or snap.proposal_hash is None
                    or snap.external_effects >= manifest.budget.max_external_effects
                    or snap.cycles_used > manifest.budget.max_cycles
                    or now >= task.deadline):
                raise StateConflict("task is not eligible for synthetic execution")
            proposal = tx.read_active_proposal()
            if (not isinstance(proposal, InternalTaskCreateProposal)
                    or sha256(proposal) != snap.proposal_hash):
                raise StateConflict("persisted internal task proposal mismatch")
            if manifest.requires_approval:
                if snap.approval_id is None:
                    raise StateConflict("approval required")
                approval = tx.read_approval(snap.approval_id)
                if (approval is None or not approval.approved
                        or approval.manifest_hash != task.manifest_hash
                        or approval.proposal_hash != snap.proposal_hash
                        or approval.task_id != task_id or now >= approval.expires_at):
                    raise StateConflict("approval no longer authorizes internal task")
            projection, simulation = tx.task_preview(proposal.simulation_ref)
            trial = snap.model_copy(update={
                "phase": OfficePhase.SIMULATED, "cycles_used": snap.cycles_used - 1,
            })
            if self._gate.evaluate(
                task=task, manifest=manifest, snapshot=trial,
                proposal=proposal, projection=projection,
                simulation=simulation, now=now,
            ).decision not in {DecisionStatus.AUTHORIZED, DecisionStatus.APPROVAL_REQUIRED}:
                raise StateConflict("synthetic internal task gate denied")
            recomputed = self._office.simulate_task(
                projection, simulation.change_diff.after, actor=task.initiator_id, now=now,
            )
            if recomputed != simulation or recomputed.conflicts:
                raise StateConflict("task simulation changed")
            customer_id = self._broker.resolve(
                manifest.customer_ref, kind=ResourceKind.CUSTOMER,
                scope=BrokerScope(
                    organization_id=task.organization_id, initiator_id=task.initiator_id,
                    task_id=task_id, operation="tasks.create", now=now,
                ),
            )
            current = reader.snapshot(organization_id=task.organization_id)
            expected_hash = self._office.trusted_task_projection_fingerprint(
                projection, now=now,
            )
            if (current.revision != projection.source_revision
                    or current.source_hash != expected_hash):
                raise StateConflict("authoritative fake task source changed")
            action_hash = sha256({
                "task_id": task_id, "manifest_hash": task.manifest_hash,
                "proposal_hash": snap.proposal_hash, "cycle": snap.cycles_used,
            })
            tx.store_protected_references(self._broker.trusted_export(task_id=task_id))
            tx.write_execution_intent(
                action_hash, snap.proposal_hash, now=now,
                operation="tasks.create", source_revision=projection.source_revision,
                source_hash=expected_hash,
            )
            tx.append(
                EventKind.ACTION_INTENT_RECORDED,
                payload={"proposal_hash": snap.proposal_hash, "action_hash": action_hash},
                actor="trusted_synthetic_gateway", now=now,
            )
            proposal_hash = snap.proposal_hash
        if enqueue_only:
            if not self._task_adapter.supports_idempotency:
                raise StateConflict("M4 task enqueue requires durable fake provider")
            return action_hash
        if self._task_adapter.supports_idempotency:
            from safe_agent.execution.recovery import SyntheticRecoveryWorker
            receipt = SyntheticRecoveryWorker(
                self._repo, self._broker, self._adapter,
                task_adapter=self._task_adapter, office=self._office,
            ).process_one(task_id=task_id, now=now)
            if receipt is None:
                raise StateConflict("enqueued task action was not claimed")
            return receipt

        effect_ref: str | None = None
        verification_hash: str | None = None
        try:
            effect_ref = writer.create(
                organization_id=task.organization_id,
                customer_id=customer_id, task_code=proposal.task_code,
                due_at=proposal.due_at, now=now, action_hash=action_hash,
                expected_revision=projection.source_revision,
                expected_source_hash=expected_hash,
            )
            observed = reader.task(organization_id=task.organization_id, task_ref=effect_ref)
            if (observed is None or observed.task_id != effect_ref
                    or observed.customer_id != customer_id
                    or observed.task_code != proposal.task_code
                    or observed.due_at != proposal.due_at or observed.status != "open"):
                raise ValueError("independent fake task postcondition failed")
            latest = reader.snapshot(organization_id=task.organization_id)
            if latest.revision == projection.source_revision:
                raise ValueError("task source revision did not advance")
            verification_hash = sha256({
                "action_hash": action_hash, "effect_ref": effect_ref,
                "observed": observed, "source_revision": latest.revision,
            })
        except Exception:
            unknown = ExecutionReceipt(
                action_hash=action_hash, manifest_hash=task.manifest_hash,
                outcome=ExecutionOutcome.OUTCOME_UNKNOWN, effect_ref=effect_ref,
            )
            with self._repo.write_task(task_id) as tx:
                if (tx.snapshot.phase is not OfficePhase.EXECUTING
                        or tx.snapshot.proposal_hash != proposal_hash):
                    raise StateConflict("internal task intent no longer owns task") from None
                tx.write_execution_outcome(action_hash, outcome="OUTCOME_UNKNOWN",
                                           receipt_json=unknown.model_dump(mode="json"))
                tx.append(
                    EventKind.ACTION_UNVERIFIED,
                    payload={"proposal_hash": proposal_hash,
                             "reason_code": "FAKE_TASK_REQUIRES_RECONCILIATION"},
                    actor="trusted_synthetic_gateway", now=now,
                )
            return unknown
        assert effect_ref is not None and verification_hash is not None
        verified = ExecutionReceipt(
            action_hash=action_hash, manifest_hash=task.manifest_hash,
            outcome=ExecutionOutcome.COMMITTED, effect_ref=effect_ref,
            verification_ref="verification_" + verification_hash[7:39],
        )
        with self._repo.write_task(task_id) as tx:
            if (tx.snapshot.phase is not OfficePhase.EXECUTING
                    or tx.snapshot.proposal_hash != proposal_hash):
                raise StateConflict("task intent no longer owns task")
            tx.write_execution_outcome(action_hash, outcome="VERIFIED",
                                       receipt_json=verified.model_dump(mode="json"))
            tx.append(
                EventKind.ACTION_VERIFIED,
                payload={"proposal_hash": proposal_hash, "effect_ref": effect_ref,
                         "verification_hash": verification_hash},
                actor="trusted_synthetic_verifier", now=now,
            )
        self._office.replace_synthetic_source(self._task_adapter.synthetic_state())
        return verified
