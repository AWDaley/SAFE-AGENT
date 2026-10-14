"""M6-01c: authenticated offline task preparation and durable synthetic queueing.

Only the deterministic FakePlanner is supported here. The browser never submits
proposal JSON, authorizes its own manifest or runs a provider worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from safe_agent.agent.fake import FakePlanner
from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.broker import ReferenceBroker
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import DecisionStatus, Task, TaskManifest
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.contracts.office import SimulatedChange
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.orchestration.runner import TaskRunner
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.virtual_office import VirtualOffice
from safe_agent.virtual_office.models import (
    InternalTaskChange,
    ObserveCalendar,
    ObserveTasks,
)


class WorkflowVersionCommand(StrictModel):
    schema_version: Literal["safe_agent.synthetic_workflow_command.v1"] = (
        "safe_agent.synthetic_workflow_command.v1"
    )
    expected_state_version: int = Field(ge=0)


class SyntheticPreparationReceipt(StrictModel):
    schema_version: Literal["safe_agent.synthetic_preparation.v1"] = (
        "safe_agent.synthetic_preparation.v1"
    )
    task_id: Identifier
    phase: Literal[OfficePhase.AWAITING_APPROVAL, OfficePhase.READY_TO_COMMIT]
    state_version: int = Field(ge=0)
    proposal_hash: Digest
    approval_required: bool
    synthetic_only: Literal[True] = True
    provider_effect_performed: Literal[False] = False


class SyntheticQueueReceipt(StrictModel):
    schema_version: Literal["safe_agent.synthetic_queue.v1"] = (
        "safe_agent.synthetic_queue.v1"
    )
    task_id: Identifier
    phase: Literal[OfficePhase.EXECUTING] = OfficePhase.EXECUTING
    state_version: int = Field(ge=0)
    queued_only: Literal[True] = True
    provider_effect_performed: Literal[False] = False


class NoPermittedSyntheticSlot(StateConflict):
    """The synthetic source currently has no valid action in the pinned window."""


class TrustedSyntheticWorkflow:
    """Connect task intake to M1/M2 contracts and M4 trusted synthetic workers.

    A separate, operator-owned SyntheticRecoveryWorker must claim queued
    actions. No execution transport is accessible through this service's API.
    """

    def __init__(
        self, repo: LedgerRepository, office: VirtualOffice,
        broker: ReferenceBroker, coordinator: TrustedOfficeTasks,
        *, gateway: SyntheticExecutionGateway | None = None,
    ) -> None:
        if gateway is not None and type(gateway) is not SyntheticExecutionGateway:
            raise TypeError("workflow queueing requires the exact synthetic gateway")
        self._repo = repo
        self._office = office
        self._broker = broker
        self._coordinator = coordinator
        self._gateway = gateway
        self._planner = TaskRunner(FakePlanner())

    def _load_assigned(
        self, principal: Principal, task_id: str, *, now: datetime, expected: int,
    ) -> tuple[Task | InternalTaskRecord, TaskManifest | InternalTaskManifest]:
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            principal.require("task:prepare", task.organization_id, now)
            principal.require("task:propose", task.organization_id, now)
            if principal.actor_id != task.initiator_id:
                raise AccessDenied("only the initiating actor may prepare this task")
            if (snap.phase is not OfficePhase.ASSIGNED
                    or snap.state_version != expected or snap.cycles_used != 0
                    or snap.external_effects != 0 or snap.proposal_hash is not None
                    or snap.simulation_ref is not None or now >= task.deadline
                    or sha256(manifest) != task.manifest_hash):
                raise StateConflict("not a current unprepared synthetic assignment")
        # Verify the independent ledger before reconstructing broker state.
        replayed = self._repo.replay_task(task_id)
        if (replayed.phase is not OfficePhase.ASSIGNED
                or replayed.state_version != expected
                or replayed.manifest_hash != task.manifest_hash):
            raise StateConflict("synthetic assignment ledger integrity failure")
        records = self._repo.load_protected_references(task_id, task.organization_id)
        expected_handles = {manifest.customer_ref}
        if isinstance(manifest, TaskManifest):
            expected_handles.add(manifest.calendar_ref)
        if (len(records) != len(expected_handles)
                or {entry.get("handle") for entry in records} != expected_handles):
            raise StateConflict("missing or unexpected synthetic private references")
        # HMAC validation, principal, expiry and operation-scoping are enforced
        # again by ReferenceBroker and VirtualOffice. A handle is not authority.
        self._broker.trusted_import(records)
        return task, manifest

    def prepare(
        self, principal: Principal, task_id: str, *,
        expected_state_version: int, now: datetime,
    ) -> SyntheticPreparationReceipt:
        now = require_aware(now)
        task, manifest = self._load_assigned(
            principal, task_id, now=now, expected=expected_state_version,
        )
        if isinstance(task, Task) and isinstance(manifest, TaskManifest):
            if manifest.permitted_operations != ("calendar.create_event",):
                raise StateConflict("unsupported synthetic calendar permission")
            try:
                duration = self._office.trusted_template_duration(
                    manifest.template_id, organization_id=task.organization_id,
                )
            except (KeyError, StopIteration, ValueError) as error:
                raise StateConflict("trusted appointment template unavailable") from error
            if duration > manifest.max_duration_minutes:
                raise StateConflict("appointment template exceeds manifest scope")
            projection = self._office.observe(ObserveCalendar(
                organization_id=task.organization_id,
                initiator_id=task.initiator_id, task_id=task_id,
                calendar_ref=manifest.calendar_ref,
                customer_ref=manifest.customer_ref,
                from_at=manifest.allowed_start, until_at=manifest.allowed_end,
            ), as_of=now)
            context = SafeTaskContext(
                task_id=task_id, organization_id=task.organization_id,
                calendar_ref=manifest.calendar_ref, customer_ref=manifest.customer_ref,
                allowed_start=manifest.allowed_start, allowed_end=manifest.allowed_end,
                duration_minutes=duration, time_zone=manifest.time_zone,
                template_id=manifest.template_id, simulation_ref="simulation_pending",
            )
            candidate = self._planner.plan_once(
                task, context, projection.to_planner_projection(),
            ).proposal
            if isinstance(candidate, RequestClarification):
                raise NoPermittedSyntheticSlot("no available synthetic appointment slot")
            change = SimulatedChange(
                calendar_ref=candidate.calendar_ref,
                customer_ref=candidate.customer_ref,
                start_at=candidate.start_at, end_at=candidate.end_at,
                template_id=candidate.template_id,
            )
            simulation = self._office.simulate(
                projection, change, actor=task.initiator_id,
                customer_ref=manifest.customer_ref, now=now,
            )
            if simulation.conflicts:
                raise NoPermittedSyntheticSlot("synthetic calendar simulation conflicts")
            proposal: CalendarCreateEvent | InternalTaskCreateProposal = CalendarCreateEvent(
                calendar_ref=candidate.calendar_ref,
                customer_ref=candidate.customer_ref,
                start_at=candidate.start_at, end_at=candidate.end_at,
                time_zone=candidate.time_zone, template_id=candidate.template_id,
                simulation_ref=simulation.simulation_ref,
                projection_rev=projection.revision,
            )
        elif isinstance(task, InternalTaskRecord) and isinstance(manifest, InternalTaskManifest):
            if (manifest.permitted_operations != ("tasks.create",)
                    or manifest.allowed_due_start <= now):
                raise NoPermittedSyntheticSlot("no permitted future internal task due date")
            projection = self._office.observe_tasks(ObserveTasks(
                organization_id=task.organization_id,
                initiator_id=task.initiator_id, task_id=task_id,
                customer_ref=manifest.customer_ref,
            ), as_of=now)
            change_task = InternalTaskChange(
                customer_ref=manifest.customer_ref,
                task_code=manifest.task_code,
                due_at=manifest.allowed_due_start,
            )
            simulation = self._office.simulate_task(
                projection, change_task, actor=task.initiator_id, now=now,
            )
            if simulation.conflicts:
                raise NoPermittedSyntheticSlot("synthetic internal task simulation conflicts")
            proposal = InternalTaskCreateProposal(
                customer_ref=change_task.customer_ref,
                task_code=change_task.task_code,
                due_at=change_task.due_at,
                simulation_ref=simulation.simulation_ref,
                projection_rev=projection.revision,
            )
        else:
            raise StateConflict("unsupported synthetic workflow domain")

        decision = self._coordinator.prepare_synthetic_atomic(
            principal, task_id, projection, simulation, proposal,
            expected_state_version=expected_state_version, now=now,
        )
        if decision.decision not in {DecisionStatus.APPROVAL_REQUIRED, DecisionStatus.AUTHORIZED}:
            raise StateConflict("trusted policy did not admit synthetic proposal")
        approval_required = decision.decision is DecisionStatus.APPROVAL_REQUIRED
        return SyntheticPreparationReceipt(
            task_id=task_id,
            phase=(OfficePhase.AWAITING_APPROVAL if approval_required
                   else OfficePhase.READY_TO_COMMIT),
            state_version=decision.state_version, proposal_hash=sha256(proposal),
            approval_required=approval_required,
        )

    def queue(
        self, principal: Principal, task_id: str, *,
        expected_state_version: int, now: datetime,
    ) -> SyntheticQueueReceipt:
        now = require_aware(now)
        if self._gateway is None:
            raise StateConflict("durable synthetic gateway not configured")
        snapshot = self._repo.replay_task(task_id)
        principal.require("task:execute", snapshot.organization_id, now)
        if principal.actor_id != snapshot.initiator_id:
            raise AccessDenied("only the task initiator may queue a synthetic effect")
        if (snapshot.phase is not OfficePhase.READY_TO_COMMIT
                or snapshot.state_version != expected_state_version):
            raise StateConflict("task is not an authorized synthetic queue candidate")
        self._gateway.enqueue(
            principal, task_id, now=now,
            expected_state_version=expected_state_version,
        )
        queued = self._repo.replay_task(task_id)
        if queued.phase is not OfficePhase.EXECUTING:
            raise StateConflict("synthetic outbox intent not recorded")
        return SyntheticQueueReceipt(
            task_id=task_id, state_version=queued.state_version,
        )
