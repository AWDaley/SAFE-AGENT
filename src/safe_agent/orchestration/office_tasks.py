"""M2 trusted state machine. Records policy eligibility but NEVER dispatches.

A verified TAL calendar domain pack and privileged action dispatcher are M3
requirements. SAFE-AGENT's model-side TaskRunner remains non-authoritative.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import Field, field_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.base import Digest, StrictModel, require_aware
from safe_agent.contracts.control import (
    ActionDecision,
    Approval,
    DecisionStatus,
    Task,
    TaskManifest,
)
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
    parse_office_proposal_json,
)
from safe_agent.contracts.operator import (
    CalendarOperatorPreview,
    InternalTaskOperatorPreview,
    OperatorInboxPage,
    OperatorProposalPreview,
)
from safe_agent.contracts.planning import PlanningLease
from safe_agent.evidence.ledger import EventKind, OfficePhase, TaskSnapshot
from safe_agent.orchestration.runner import PlanningResult
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import VirtualOffice
from safe_agent.virtual_office.models import (
    CalendarProjection,
    CalendarSimulation,
    InternalTaskSimulation,
    TaskListProjection,
)

if TYPE_CHECKING:
    from safe_agent.execution.synthetic import SyntheticExecutionGateway


class ApprovalInput(StrictModel):
    proposal_hash: Digest
    expected_state_version: int = Field(ge=0)
    approved: bool
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def aware(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_aware(value)


class TrustedOfficeTasks:
    """Coordinator for authority, durable ledger, and explicit approvals.

    Important: `AUTHORIZED` means an offline proposal satisfied local policy.
    Neither this service nor its decisions are callable as provider commits.
    """

    def __init__(self, repo: LedgerRepository, office: VirtualOffice, gate: OfficeGate,
                 *, synthetic_gateway: SyntheticExecutionGateway | None = None):
        self._repo = repo
        self._office = office
        self._gate = gate
        self._synthetic_gateway = synthetic_gateway

    def register(self, principal: Principal, task: Task | InternalTaskRecord,
                 manifest: TaskManifest | InternalTaskManifest,
                 *, now: datetime,
                 protected_references: tuple[dict[str, object], ...] = (),
                 intake_key: str | None = None,
                 intake_hash: str | None = None) -> TaskSnapshot:
        now = require_aware(now)
        principal.require("task:create", task.organization_id, now)
        if principal.actor_id != task.initiator_id:
            raise AccessDenied("task initiator mismatch")
        if (task.task_id != manifest.task_id or task.manifest_hash != sha256(manifest)
                or task.manifest_id != manifest.manifest_id
                or task.organization_id != manifest.organization_id
                or task.initiator_id != manifest.initiator_id):
            raise ValueError("task/manifest authority does not match")
        if not task.created_at <= now <= task.deadline:
            raise ValueError("task is outside its permitted lifetime")
        return self._repo.create_task(
            task, manifest, actor=principal.actor_id, now=now,
            protected_references=protected_references,
            intake_key=intake_key, intake_hash=intake_hash,
        )

    def preview(self, principal: Principal, task_id: str,
                projection: CalendarProjection, simulation: CalendarSimulation,
                *, expected_state_version: int, now: datetime) -> TaskSnapshot:
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task = tx.task
            manifest = tx.manifest
            principal.require("task:prepare", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("cannot prepare another actor's task")
            if expected_state_version != tx.snapshot.state_version:
                raise StateConflict("stale task state version")
            if not isinstance(manifest, TaskManifest):
                raise StateConflict("wrong domain for calendar preview")
            if (projection.organization_id != task.organization_id
                    or projection.task_id != task_id or projection.calendar_ref != manifest.calendar_ref
                    or simulation.projection_rev != projection.revision
                    or simulation.conflicts or simulation.advisory_only is not True):
                raise StateConflict("invalid or conflicting preview")
            if not projection.fetched_at <= now < projection.expires_at:
                raise StateConflict("preview expired")
            change = simulation.change_diff.after
            if (change.calendar_ref != manifest.calendar_ref
                    or change.customer_ref != manifest.customer_ref
                    or change.template_id != manifest.template_id):
                raise StateConflict("preview not within manifest scope")
            # The model cannot forge valid previews: recompute through trusted
            # VirtualOffice issuance cache and current synthetic source.
            check = self._office.simulate(
                projection, change, actor=task.initiator_id,
                customer_ref=manifest.customer_ref, now=now,
            )
            if check != simulation or check.simulation_hash != simulation.simulation_hash:
                raise StateConflict("preview fails independent simulation")
            tx.write_preview(projection, simulation, now=now)
            tx.append(EventKind.PREVIEW_REGISTERED, actor=principal.actor_id, now=now,
                      payload={"simulation_ref": simulation.simulation_ref,
                               "simulation_hash": simulation.simulation_hash,
                               "projection_rev": projection.revision})
            return tx.snapshot

    def preview_internal_task(
        self, principal: Principal, task_id: str,
        projection: TaskListProjection, simulation: InternalTaskSimulation,
        *, expected_state_version: int, now: datetime,
    ) -> TaskSnapshot:
        """Trusted synthetic internal-task preview via the existing ledger plane."""
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            principal.require("task:prepare", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("cannot prepare another actor's task")
            if not isinstance(manifest, InternalTaskManifest):
                raise StateConflict("wrong domain for task preview")
            if expected_state_version != snap.state_version:
                raise StateConflict("stale task state version")
            if (projection.organization_id != task.organization_id
                    or projection.task_id != task_id
                    or projection.customer_ref != manifest.customer_ref
                    or simulation.projection_rev != projection.revision
                    or simulation.conflicts or simulation.advisory_only is not True
                    or not projection.fetched_at <= now < projection.expires_at):
                raise StateConflict("internal task preview rejected")
            change = simulation.change_diff.after
            if (change.customer_ref != manifest.customer_ref
                    or change.task_code != manifest.task_code):
                raise StateConflict("task preview outside manifest scope")
            verified = self._office.simulate_task(projection, change,
                                                   actor=task.initiator_id, now=now)
            if verified != simulation:
                raise StateConflict("task preview fails independent simulation")
            tx.write_preview(projection, simulation, now=now)
            tx.append(EventKind.PREVIEW_REGISTERED, actor=principal.actor_id, now=now,
                      payload={"simulation_ref": simulation.simulation_ref,
                               "simulation_hash": simulation.simulation_hash,
                               "projection_rev": projection.revision})
            return tx.snapshot

    def propose(self, principal: Principal, task_id: str, proposal_json: str,
                *, expected_state_version: int, now: datetime) -> ActionDecision:
        now = require_aware(now)
        # JSON parsing is closed; malformed text never enters event logs.
        try:
            parsed = parse_office_proposal_json(proposal_json)
            proposal_hash = sha256(parsed)
        except (ValueError, TypeError):
            parsed = None
            proposal_hash = sha256({"invalid_proposal": True})
        with self._repo.write_task(task_id) as tx:
            task, snap = tx.task, tx.snapshot
            principal.require("task:propose", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("proposal not authorized for actor")

            def reject(reason: str) -> ActionDecision:
                tx.append(EventKind.PROPOSAL_DENIED, payload={
                    "reason_code": reason, "proposal_hash": proposal_hash,
                }, actor=principal.actor_id, now=now)
                return ActionDecision(
                    decision=DecisionStatus.DENIED, manifest_hash=task.manifest_hash,
                    proposal_hash=proposal_hash, reason_code=reason,
                    state_version=snap.state_version,
                )

            if expected_state_version != snap.state_version:
                return reject("STALE_TASK_VERSION")
            if parsed is None:
                return reject("INVALID_PROPOSAL_SCHEMA")
            if isinstance(parsed, RequestClarification):
                return reject("CLARIFICATION_REQUIRED")
            assert isinstance(parsed, CalendarCreateEvent | InternalTaskCreateProposal)
            if snap.simulation_ref != parsed.simulation_ref:
                return reject("SIMULATION_NOT_ACTIVE")
            projection: CalendarProjection | TaskListProjection
            simulation: CalendarSimulation | InternalTaskSimulation
            try:
                if isinstance(parsed, InternalTaskCreateProposal):
                    projection, simulation = tx.task_preview(parsed.simulation_ref)
                else:
                    projection, simulation = tx.preview(parsed.simulation_ref)
            except (StateConflict, ValueError):
                return reject("SIMULATION_NOT_REGISTERED")
            decision = self._gate.evaluate(
                task=task, manifest=tx.manifest, snapshot=snap,
                proposal=parsed, projection=projection, simulation=simulation, now=now,
            )
            if decision.decision is DecisionStatus.DENIED:
                return reject(decision.reason_code)
            tx.write_proposal(snap.cycles_used + 1, parsed, parsed.simulation_ref, now=now)
            tx.append(EventKind.PROPOSAL_ACCEPTED, payload={
                "proposal_hash": proposal_hash, "simulation_ref": parsed.simulation_ref,
            }, actor=principal.actor_id, now=now)
            if decision.decision is DecisionStatus.APPROVAL_REQUIRED:
                tx.append(EventKind.APPROVAL_REQUIRED, payload={"proposal_hash": proposal_hash},
                          actor=principal.actor_id, now=now)
            else:
                tx.append(EventKind.ACTION_ELIGIBLE, payload={"proposal_hash": proposal_hash},
                          actor="offline_office_gate", now=now)
            return decision.model_copy(update={"state_version": tx.snapshot.state_version})

    def prepare_synthetic_atomic(
        self, principal: Principal, task_id: str,
        projection: CalendarProjection | TaskListProjection,
        simulation: CalendarSimulation | InternalTaskSimulation,
        proposal: CalendarCreateEvent | InternalTaskCreateProposal,
        *, expected_state_version: int, now: datetime,
        planning_lease: PlanningLease | None = None,
    ) -> ActionDecision:
        """Atomically register one trusted synthetic preview and gated proposal.

        The existing two-step M2 API remains for compatibility. This path is
        restricted to fresh, assigned tasks so a crash cannot leave a newly
        onboarded task with an orphaned SIMULATED preview.
        """
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            principal.require("task:prepare", task.organization_id, now)
            principal.require("task:propose", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("only the task initiator may prepare a proposal")
            # A claimed planning workspace is not an alternative grant. No
            # unmetered direct preparation may bypass its durable attempt budget.
            from safe_agent.storage.models import PlanningWorkspaceRow

            if (planning_lease is None
                    and tx.session.get(PlanningWorkspaceRow, task_id) is not None):
                raise StateConflict("planning workspace requires a bounded attempt")
            if (snap.phase is not OfficePhase.ASSIGNED
                    or snap.state_version != expected_state_version
                    or snap.cycles_used != 0 or snap.external_effects != 0
                    or snap.simulation_ref is not None or snap.proposal_hash is not None
                    or task.manifest_hash != sha256(manifest)
                    or now >= task.deadline):
                raise StateConflict("task is not an untouched, authorized assignment")
            checked: CalendarSimulation | InternalTaskSimulation
            if isinstance(proposal, CalendarCreateEvent):
                if (not isinstance(task, Task)
                        or not isinstance(manifest, TaskManifest)
                        or not isinstance(projection, CalendarProjection)
                        or not isinstance(simulation, CalendarSimulation)):
                    raise StateConflict("calendar preparation domain mismatch")
                diff = simulation.change_diff.after
                if (projection.organization_id != task.organization_id
                        or projection.task_id != task_id
                        or projection.calendar_ref != manifest.calendar_ref
                        or not projection.fetched_at <= now < projection.expires_at
                        or simulation.conflicts or not simulation.advisory_only
                        or simulation.projection_rev != projection.revision
                        or proposal.projection_rev != projection.revision
                        or proposal.simulation_ref != simulation.simulation_ref
                        or proposal.calendar_ref != diff.calendar_ref
                        or proposal.customer_ref != diff.customer_ref
                        or proposal.start_at != diff.start_at
                        or proposal.end_at != diff.end_at
                        or proposal.template_id != diff.template_id):
                    raise StateConflict("synthetic calendar proposal/preview mismatch")
                checked = self._office.simulate(
                    projection, diff, actor=task.initiator_id,
                    customer_ref=manifest.customer_ref, now=now,
                )
            else:
                if (not isinstance(task, InternalTaskRecord)
                        or not isinstance(manifest, InternalTaskManifest)
                        or not isinstance(projection, TaskListProjection)
                        or not isinstance(simulation, InternalTaskSimulation)):
                    raise StateConflict("internal task preparation domain mismatch")
                diff_task = simulation.change_diff.after
                if (projection.organization_id != task.organization_id
                        or projection.task_id != task_id
                        or projection.customer_ref != manifest.customer_ref
                        or not projection.fetched_at <= now < projection.expires_at
                        or simulation.conflicts or not simulation.advisory_only
                        or simulation.projection_rev != projection.revision
                        or proposal.projection_rev != projection.revision
                        or proposal.simulation_ref != simulation.simulation_ref
                        or proposal.customer_ref != diff_task.customer_ref
                        or proposal.task_code != diff_task.task_code
                        or proposal.due_at != diff_task.due_at):
                    raise StateConflict("synthetic internal task proposal/preview mismatch")
                checked = self._office.simulate_task(
                    projection, diff_task, actor=task.initiator_id, now=now,
                )
            if checked != simulation or checked.conflicts:
                raise StateConflict("trusted synthetic simulation changed")
            # All following writes and ledger transitions share this SQL transaction.
            tx.write_preview(projection, simulation, now=now)
            tx.append(EventKind.PREVIEW_REGISTERED, actor=principal.actor_id, now=now,
                      payload={"simulation_ref": simulation.simulation_ref,
                               "simulation_hash": simulation.simulation_hash,
                               "projection_rev": projection.revision})
            decision = self._gate.evaluate(
                task=task, manifest=manifest, snapshot=tx.snapshot,
                proposal=proposal, projection=projection,
                simulation=simulation, now=now,
            )
            if decision.decision is DecisionStatus.DENIED:
                raise StateConflict("synthetic proposal denied by trusted gate")
            tx.write_proposal(tx.snapshot.cycles_used + 1, proposal,
                              proposal.simulation_ref, now=now)
            digest = sha256(proposal)
            tx.append(EventKind.PROPOSAL_ACCEPTED, actor=principal.actor_id, now=now,
                      payload={"proposal_hash": digest,
                               "simulation_ref": proposal.simulation_ref})
            if decision.decision is DecisionStatus.APPROVAL_REQUIRED:
                tx.append(EventKind.APPROVAL_REQUIRED, actor=principal.actor_id, now=now,
                          payload={"proposal_hash": digest})
            else:
                tx.append(EventKind.ACTION_ELIGIBLE, actor="offline_office_gate", now=now,
                          payload={"proposal_hash": digest})
            if planning_lease is not None:
                from safe_agent.storage.planning import finish_planning_attempt

                finish_planning_attempt(
                    tx, planning_lease, result_kind="PROPOSED",
                    reason_code=None, proposal_hash=digest, now=now,
                )
            return decision.model_copy(update={"state_version": tx.snapshot.state_version})

    def approve(self, principal: Principal, task_id: str, request: ApprovalInput,
                *, now: datetime) -> ActionDecision:
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task, snap, manifest = tx.task, tx.snapshot, tx.manifest
            principal.require("task:approve", task.organization_id, now)
            if principal.actor_id == task.initiator_id:
                raise AccessDenied("separation of duties: cannot self-approve")
            if (not manifest.requires_approval or snap.phase is not OfficePhase.AWAITING_APPROVAL
                    or snap.state_version != request.expected_state_version
                    or snap.proposal_hash != request.proposal_hash
                    or now > task.deadline or request.expires_at <= now
                    or request.expires_at > task.deadline):
                raise StateConflict("approval no longer matches active authority")
            approval_id = "approval_" + sha256({
                "task": task_id, "proposal": request.proposal_hash,
                "approver": principal.actor_id, "approved": request.approved,
                "expires_at": request.expires_at,
            })[7:37]
            receipt = Approval(
                approval_id=approval_id, task_id=task_id, approver_id=principal.actor_id,
                proposal_hash=request.proposal_hash, manifest_hash=task.manifest_hash,
                expires_at=request.expires_at, approved=request.approved,
            )
            tx.write_approval(receipt, organization_id=task.organization_id,
                              approver_id=principal.actor_id, now=now)
            kind = EventKind.APPROVAL_GRANTED if request.approved else EventKind.APPROVAL_DENIED
            tx.append(kind, payload={"approval_id": approval_id,
                                     "proposal_hash": request.proposal_hash},
                      actor=principal.actor_id, now=now)
            return ActionDecision(
                decision=DecisionStatus.AUTHORIZED if request.approved else DecisionStatus.DENIED,
                manifest_hash=task.manifest_hash, proposal_hash=request.proposal_hash,
                reason_code="OFFLINE_APPROVED_NOT_UPSTREAM_TAL" if request.approved
                            else "OPERATOR_DENIED", state_version=tx.snapshot.state_version,
            )

    def submit_planning_result(self, principal: Principal, task_id: str,
                               result: PlanningResult, *, expected_state_version: int,
                               now: datetime) -> ActionDecision:
        """M2 connector from the unprivileged M0/M1 TaskRunner; no effects."""
        if (result.task_id != task_id or sha256(result.proposal) != result.proposal_hash
                or result.explicitly_not_committed is not True):
            raise StateConflict("planner result is not bound to the task/proposal")
        return self.propose(
            principal, task_id, result.proposal.model_dump_json(),
            expected_state_version=expected_state_version, now=now,
        )

    def revalidate_offline_readiness(self, principal: Principal, task_id: str,
                                     *, now: datetime) -> ActionDecision:
        """Recheck expired approvals/projections/handles; still NEVER dispatch.

        In M3 this must be supplemented with authoritative source read-back,
        provider conditional writes, and actual upstream TAL office-domain gate.
        """
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task, snap, manifest = tx.task, tx.snapshot, tx.manifest
            principal.require("task:read", task.organization_id, now)

            def deny(code: str) -> ActionDecision:
                return ActionDecision(
                    decision=DecisionStatus.DENIED, manifest_hash=task.manifest_hash,
                    proposal_hash=snap.proposal_hash or sha256({"no_proposal": True}),
                    reason_code=code, state_version=snap.state_version,
                )

            if snap.phase is not OfficePhase.READY_TO_COMMIT or snap.proposal_hash is None:
                return deny("NOT_READY")
            proposal = tx.read_active_proposal()
            if proposal is None:
                return deny("PROPOSAL_RECORD_MISSING")
            if manifest.requires_approval:
                if snap.approval_id is None:
                    return deny("APPROVAL_MISSING")
                approval = tx.read_approval(snap.approval_id)
                if (approval is None or not approval.approved
                        or approval.task_id != task_id
                        or approval.manifest_hash != task.manifest_hash
                        or approval.proposal_hash != snap.proposal_hash
                        or now >= approval.expires_at):
                    return deny("APPROVAL_EXPIRED_OR_INVALID")
            projection: CalendarProjection | TaskListProjection
            simulation: CalendarSimulation | InternalTaskSimulation
            if isinstance(proposal, InternalTaskCreateProposal):
                projection, simulation = tx.task_preview(proposal.simulation_ref)
            else:
                projection, simulation = tx.preview(proposal.simulation_ref)
            hypothetical = snap.model_copy(update={
                "phase": OfficePhase.SIMULATED,
                "cycles_used": snap.cycles_used - 1,
            })
            result = self._gate.evaluate(
                task=task, manifest=manifest, snapshot=hypothetical,
                proposal=proposal, projection=projection, simulation=simulation, now=now,
            )
            if result.decision is DecisionStatus.DENIED:
                return result.model_copy(update={"state_version": snap.state_version})
            return ActionDecision(
                decision=DecisionStatus.AUTHORIZED, manifest_hash=task.manifest_hash,
                proposal_hash=snap.proposal_hash,
                reason_code="OFFLINE_READY_NOT_EXECUTABLE",
                state_version=snap.state_version,
            )

    def operator_inbox(
        self, principal: Principal, *, now: datetime,
        after: str | None = None, limit: int = 20,
    ) -> OperatorInboxPage:
        """Read-only, tenant-filtered operator inbox, requiring task:read."""
        now = require_aware(now)
        principal.require("task:read", principal.organization_id, now)
        return self._repo.list_operator_tasks(
            principal.organization_id, after=after, limit=limit,
        )

    def operator_proposal_preview(
        self, principal: Principal, task_id: str, *, now: datetime,
    ) -> OperatorProposalPreview:
        """Preview only the persisted, scoped synthetic proposed change.

        The preview is advisory, not a claim that Google has reserved a slot.
        No new proposal, approval, external action or broker resolve occurs.
        """
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            task, snapshot, manifest = tx.task, tx.snapshot, tx.manifest
            principal.require("task:read", task.organization_id, now)
            if (snapshot.phase not in {
                OfficePhase.AWAITING_APPROVAL, OfficePhase.READY_TO_COMMIT,
                OfficePhase.EXECUTING, OfficePhase.COMPLETED, OfficePhase.NEEDS_REVIEW,
            } or snapshot.proposal_hash is None):
                raise StateConflict("no reviewable proposal for this task")
            proposal = tx.read_active_proposal()
            if proposal is None or sha256(proposal) != snapshot.proposal_hash:
                raise StateConflict("operator preview has no trusted proposal")
            if isinstance(proposal, CalendarCreateEvent):
                if not isinstance(manifest, TaskManifest):
                    raise StateConflict("calendar proposal manifest mismatch")
                projection, simulation = tx.preview(proposal.simulation_ref)
                change = simulation.change_diff.after
                if (proposal.projection_rev != projection.revision
                        or proposal.simulation_ref != simulation.simulation_ref
                        or simulation.conflicts
                        or simulation.expected_postconditions != (
                            "event_created", "matching_time")
                        or proposal.calendar_ref != manifest.calendar_ref
                        or proposal.customer_ref != manifest.customer_ref
                        or proposal.template_id != manifest.template_id
                        or change.calendar_ref != proposal.calendar_ref
                        or change.customer_ref != proposal.customer_ref
                        or change.template_id != proposal.template_id
                        or change.start_at != proposal.start_at
                        or change.end_at != proposal.end_at):
                    raise StateConflict("operator calendar preview not verified")
                return CalendarOperatorPreview(
                    task_id=task_id, proposal_hash=snapshot.proposal_hash,
                    state_version=snapshot.state_version, phase=snapshot.phase,
                    calendar_ref=proposal.calendar_ref, customer_ref=proposal.customer_ref,
                    template_id=proposal.template_id,
                    start_at=proposal.start_at, end_at=proposal.end_at,
                    time_zone=proposal.time_zone,
                    approval_required=manifest.requires_approval,
                )
            if isinstance(proposal, InternalTaskCreateProposal):
                if not isinstance(manifest, InternalTaskManifest):
                    raise StateConflict("internal proposal manifest mismatch")
                task_projection, task_simulation = tx.task_preview(proposal.simulation_ref)
                task_change = task_simulation.change_diff.after
                if (proposal.projection_rev != task_projection.revision
                        or proposal.simulation_ref != task_simulation.simulation_ref
                        or task_simulation.conflicts
                        or task_simulation.expected_postconditions != ("task_created",)
                        or proposal.customer_ref != manifest.customer_ref
                        or proposal.task_code != manifest.task_code
                        or task_change.customer_ref != proposal.customer_ref
                        or task_change.task_code != proposal.task_code
                        or task_change.due_at != proposal.due_at):
                    raise StateConflict("operator internal preview not verified")
                return InternalTaskOperatorPreview(
                    task_id=task_id, proposal_hash=snapshot.proposal_hash,
                    state_version=snapshot.state_version, phase=snapshot.phase,
                    customer_ref=proposal.customer_ref, task_code=proposal.task_code,
                    due_at=proposal.due_at, approval_required=manifest.requires_approval,
                )
            raise StateConflict("unsupported operator preview operation")

    def inspect(self, principal: Principal, task_id: str, *, now: datetime) -> TaskSnapshot:
        state = self._repo.read_task(task_id)
        principal.require("task:read", state.organization_id, now)
        return state

    def events(self, principal: Principal, task_id: str, *, now: datetime):
        state = self._repo.read_task(task_id)
        principal.require("task:audit", state.organization_id, now)
        return self._repo.read_events(task_id)

    def execution_status(self, principal: Principal, task_id: str, *,
                         now: datetime) -> dict[str, object]:
        """Read-only, tenant-scoped outbox status for operator escalation."""
        snap = self._repo.read_task(task_id)
        principal.require("task:read", snap.organization_id, require_aware(now))
        return self._repo.read_outbox(task_id)

    def replay(self, principal: Principal, task_id: str, *, now: datetime) -> TaskSnapshot:
        state = self._repo.read_task(task_id)
        principal.require("task:audit", state.organization_id, now)
        return self._repo.replay_task(task_id)

    def execute_synthetic(self, principal: Principal, task_id: str, *,
                          expected_state_version: int, now: datetime):
        """Only the installed fake gateway can execute a synthetic eligible action."""
        from safe_agent.contracts.control import ExecutionReceipt
        if self._synthetic_gateway is None:
            raise StateConflict("synthetic execution not configured")
        receipt: ExecutionReceipt = self._synthetic_gateway.execute(
            principal, task_id, now=now, expected_state_version=expected_state_version,
        )
        return receipt
