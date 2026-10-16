"""W4 calendar planning: durable input -> coordinator -> atomic gated consumption.

The supported operator path accepts only version commands. No model callbacks,
mode selection, raw results, approvals or provider writes are accepted here.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect, select

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.broker import BrokerScope, ResourceKind
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import CalendarCreateEvent, RequestClarification
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import DecisionStatus, Task, TaskManifest
from safe_agent.contracts.coordinated_planning import CoordinatedPlanningInput
from safe_agent.contracts.office import SimulatedChange
from safe_agent.contracts.planning import PlanningCommand, PlanningLease, PlanningReceipt
from safe_agent.evidence.ledger import EventKind
from safe_agent.inference.bootstrap import create_application_inference
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.gateway import _check_candidate
from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.coordinated_models import (
    CoordinatedPlanningConsumptionRow as ConsumptionRow,
)
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow as InputRow
from safe_agent.storage.coordinated_planning import bound_result, read_consumption, read_input
from safe_agent.storage.inference_admission import (
    TrustedInferenceAdmission,
    _live_attempt,
    _task_identity,
)
from safe_agent.storage.models import (
    InferenceInvocationRow,
    PlanningAttemptRow,
    PlanningWorkspaceRow,
    ReferenceRow,
)
from safe_agent.storage.planning import (
    PlanningWorkspaceStore,
    _assert_workspace,
    _utc,
    finish_planning_attempt,
)
from safe_agent.storage.repository import StateConflict, TaskTransaction
from safe_agent.virtual_office.models import ObserveCalendar


class TrustedCoordinatedPlanning(TrustedPersistentPlanning):
    """Explicit opt-in replacement for legacy fixture planning on the same API.

    Each task has one immutable coordinated input/attempt. Pending, unknown,
    stale and missing results never cause a new attempt or a fallback planner.
    Existing /planning metadata and /plan's fixed 409 represent unresolved work.
    """

    def __init__(self, store: PlanningWorkspaceStore, workflow: TrustedSyntheticWorkflow,
                 *, inference: TrustedInferenceCoordinator) -> None:
        if (type(store) is not PlanningWorkspaceStore or type(workflow) is not TrustedSyntheticWorkflow
                or type(inference) is not TrustedInferenceCoordinator):
            raise TypeError("exact coordinated services required")
        repo = store._repo
        gateway = workflow._gateway
        if (inference.store.admission._repo is not repo or workflow._repo is not repo
                or workflow._coordinator._repo is not repo
                or workflow._coordinator._office is not workflow._office
                or workflow._office._broker is not workflow._broker
                or gateway is None or gateway._repo is not repo
                or gateway._office is not workflow._office or gateway._broker is not workflow._broker):
            raise TypeError("coordinated services must share trusted repository, office and broker")
        required = {InputRow.__tablename__, ConsumptionRow.__tablename__}
        if not required <= set(inspect(repo.engine).get_table_names()):
            raise StateConflict("COORDINATED_SCHEMA_REQUIRED")
        # Do not call the legacy constructor: there is no fallback FakePlanner
        # or stateless generate callback stored by this implementation.
        self._store = store
        self._workflow = workflow
        self._inference = inference
        self._mode = "text_only"
        self._clock = inference.store.admission._clock

    def _now(self) -> datetime:
        return require_aware(self._clock()).astimezone(UTC)

    @staticmethod
    def _command(body: PlanningCommand, lease: PlanningLease, workspace_version: int) -> None:
        if (body.expected_state_version != lease.task_state_version
                or body.expected_workspace_version not in {
                    lease.workspace_version - 1, lease.workspace_version, workspace_version,
                }):
            raise StateConflict("COORDINATED_COMMAND_STALE")

    def _references(self, tx: TaskTransaction, value: CoordinatedPlanningInput) -> None:
        rows = tx.session.scalars(select(ReferenceRow).where(
            ReferenceRow.task_id == value.lease.task_id,
        ).with_for_update()).all()
        handles = {value.context.calendar_ref, value.context.customer_ref}
        if len(rows) != 2 or {row.handle for row in rows} != handles:
            raise StateConflict("COORDINATED_REFERENCES_DENIED")
        for row in rows:
            if (row.revoked or row.organization_id != tx.task.organization_id
                    or row.initiator_id != tx.task.initiator_id
                    or row.binding_json.get("handle") != row.handle):
                raise StateConflict("COORDINATED_REFERENCES_DENIED")
        self._workflow._broker.trusted_import(tuple(row.binding_json for row in rows))

    def _source(self, tx: TaskTransaction, value: CoordinatedPlanningInput, now: datetime) -> None:
        """Reissue ONLY the identical original snapshot, never extend freshness.

        Both the trusted synthetic cache and independent fake-provider read must
        match the original content hash, even when a source revision is unchanged.
        """
        self._references(tx, value)
        projection, ctx = value.projection, value.context
        if not projection.fetched_at <= now < projection.expires_at:
            raise StateConflict("COORDINATED_SOURCE_STALE")
        office = self._workflow._office
        checked = office.observe(ObserveCalendar(
            organization_id=ctx.organization_id, initiator_id=tx.task.initiator_id,
            task_id=ctx.task_id, calendar_ref=ctx.calendar_ref, customer_ref=ctx.customer_ref,
            from_at=ctx.allowed_start, until_at=ctx.allowed_end,
        ), as_of=projection.fetched_at)
        if (checked != projection or office.trusted_projection_fingerprint(checked, now=now)
                != value.source_fingerprint
                or office.trusted_template_duration(ctx.template_id, organization_id=ctx.organization_id)
                != ctx.duration_minutes):
            raise StateConflict("COORDINATED_SOURCE_CHANGED")
        scope = BrokerScope(organization_id=ctx.organization_id, initiator_id=tx.task.initiator_id,
                            task_id=ctx.task_id, operation="calendar.create_event", now=now)
        broker = self._workflow._broker
        calendar_id = broker.resolve(ctx.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR)
        broker.resolve(ctx.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER)
        gateway = self._workflow._gateway
        if gateway is None:
            raise StateConflict("COORDINATED_SYNTHETIC_READER_REQUIRED")
        current = gateway._reader.snapshot(calendar_id=calendar_id, organization_id=ctx.organization_id)
        if (current.source_hash != value.source_fingerprint
                or current.calendar.source_revision != projection.source_revision
                or current.calendar.time_zone != ctx.time_zone):
            raise StateConflict("COORDINATED_PROVIDER_SOURCE_CHANGED")

    def _input(self, principal: Principal, task_id: str, body: PlanningCommand) -> CoordinatedPlanningInput:
        lease: PlanningLease | None = None
        now = self._now()
        with self._store._repo.write_task(task_id, serialize_sqlite=True) as tx:
            principal.require("task:prepare", tx.task.organization_id, now)
            principal.require("task:propose", tx.task.organization_id, now)
            if principal.actor_id != tx.task.initiator_id:
                raise AccessDenied("task initiator required")
            stored = tx.session.scalar(select(InputRow).where(InputRow.task_id == task_id))
            workspace = tx.session.get(PlanningWorkspaceRow, task_id)
            if stored is not None:
                value = read_input(stored)
                if workspace is None or value.policy_hash != self._inference.store.admission._policy_hash:
                    raise StateConflict("COORDINATED_INPUT_DENIED")
                _assert_workspace(tx, workspace)
                self._command(body, value.lease, workspace.workspace_version)
                _task_identity(tx, principal, value.lease, value.context, now)
                return value
            # Do not adopt a legacy invocation or regenerate its missing input.
            if tx.session.scalar(select(InferenceInvocationRow.request_id).where(
                InferenceInvocationRow.task_id == task_id,
            ).limit(1)) is not None:
                raise StateConflict("COORDINATED_INPUT_MISSING")
            if workspace is not None:
                _assert_workspace(tx, workspace)
                attempt = tx.session.get(PlanningAttemptRow, workspace.active_attempt_id) if workspace.active_attempt_id else None
                if attempt is None or workspace.mode != "text_only" or workspace.lease_until is None:
                    raise StateConflict("COORDINATED_WORKSPACE_DENIED")
                lease = PlanningLease(task_id=task_id, attempt_id=attempt.attempt_id,
                                      workspace_version=workspace.workspace_version,
                                      attempt_number=attempt.attempt_number,
                                      task_state_version=attempt.task_state_version,
                                      manifest_hash=attempt.manifest_hash, mode="text_only",
                                      expires_at=_utc(attempt.lease_until))
                self._command(body, lease, workspace.workspace_version)
        if lease is None:
            lease = self._store.begin(principal, task_id, expected_task_version=body.expected_state_version,
                                      expected_workspace_version=body.expected_workspace_version,
                                      mode="text_only", now=now)
        if lease is None:
            raise StateConflict("COORDINATED_ATTEMPTS_EXHAUSTED")
        task, manifest = self._workflow._load_assigned(principal, task_id, now=self._now(),
                                                     expected=lease.task_state_version)
        if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
                or manifest.permitted_operations != ("calendar.create_event",) or not manifest.requires_approval):
            raise StateConflict("COORDINATED_APPROVED_CALENDAR_ONLY")
        now = self._now()
        office = self._workflow._office
        projection = office.observe(ObserveCalendar(
            organization_id=task.organization_id, initiator_id=task.initiator_id, task_id=task_id,
            calendar_ref=manifest.calendar_ref, customer_ref=manifest.customer_ref,
            from_at=manifest.allowed_start, until_at=manifest.allowed_end,
        ), as_of=now)
        context = SafeTaskContext(task_id=task_id, organization_id=task.organization_id,
                                  calendar_ref=manifest.calendar_ref, customer_ref=manifest.customer_ref,
                                  allowed_start=manifest.allowed_start, allowed_end=manifest.allowed_end,
                                  duration_minutes=office.trusted_template_duration(manifest.template_id,
                                                                                  organization_id=task.organization_id),
                                  time_zone=manifest.time_zone, template_id=manifest.template_id,
                                  simulation_ref="simulation_pending")
        value = CoordinatedPlanningInput(lease=lease, policy_hash=self._inference.store.admission._policy_hash,
                                         context=context, projection=projection,
                                         source_fingerprint=office.trusted_projection_fingerprint(projection, now=now))
        with self._store._repo.write_task(task_id, serialize_sqlite=True) as tx:
            existing = tx.session.get(InputRow, lease.attempt_id)
            if existing is not None:
                # A competing caller's first committed input wins. Never overwrite.
                saved = read_input(existing)
                if saved.lease != lease or saved.policy_hash != value.policy_hash:
                    raise StateConflict("COORDINATED_INPUT_DENIED")
                _task_identity(tx, principal, lease, saved.context, self._now())
                return saved
            _task_identity(tx, principal, lease, context, self._now())
            _live_attempt(tx, lease, self._now())
            self._source(tx, value, self._now())
            tx.session.add(InputRow(attempt_id=lease.attempt_id, task_id=task_id,
                                    organization_id=task.organization_id, manifest_hash=lease.manifest_hash,
                                    task_state_version=lease.task_state_version, workspace_version=lease.workspace_version,
                                    policy_hash=value.policy_hash, input_hash=value.input_hash,
                                    record_json=canonical_json(value).decode("utf-8"), recorded_at=now))
            tx.session.flush()
            _live_attempt(tx, lease, self._now())
        return value

    def _consume(self, principal: Principal, value: CoordinatedPlanningInput) -> None:
        store = self._inference.store
        with store._locked(principal, value.lease, value.prompt()) as locked:
            tx, now = locked.tx, locked.now
            raw = tx.session.get(InputRow, value.lease.attempt_id)
            if raw is None or read_input(raw) != value:
                raise StateConflict("COORDINATED_INPUT_DENIED")
            consumed = read_consumption(tx, value)
            if consumed is not None:
                # Restore the issuance cache using ORIGINAL bounds, before a
                # later queue operation. Completed business effects are not replayed.
                if consumed.result_kind == "PROPOSED" and now < value.projection.expires_at:
                    self._source(tx, value, now)
                return
            store._live(locked, value.lease)
            record = bound_result(tx, value)
            if record.binding != locked.receipt.binding or record.request != locked.request:
                raise StateConflict("COORDINATED_RESULT_BINDING_DENIED")
            self._source(tx, value, now)
            candidate = _check_candidate(record.outcome.proposal, value.context,
                                         value.projection.to_planner_projection())
            manifest = tx.manifest
            if not isinstance(manifest, TaskManifest) or not manifest.requires_approval:
                raise StateConflict("COORDINATED_HUMAN_APPROVAL_REQUIRED")
            if isinstance(candidate, RequestClarification):
                finish_planning_attempt(tx, value.lease, result_kind="NEEDS_CLARIFICATION",
                                        reason_code=candidate.reason_code, proposal_hash=None, now=now,
                                        coordinated_result_hash=record.result_hash)
            else:
                change = SimulatedChange(calendar_ref=candidate.calendar_ref, customer_ref=candidate.customer_ref,
                                         start_at=candidate.start_at, end_at=candidate.end_at,
                                         template_id=candidate.template_id)
                simulation = self._workflow._office.simulate(value.projection, change,
                    actor=principal.actor_id, customer_ref=candidate.customer_ref, now=now)
                if simulation.conflicts:
                    raise StateConflict("COORDINATED_SIMULATION_DENIED")
                final = CalendarCreateEvent.model_validate({**candidate.model_dump(),
                                                           "simulation_ref": simulation.simulation_ref})
                tx.write_preview(value.projection, simulation, now=now)
                tx.append(EventKind.PREVIEW_REGISTERED, actor=principal.actor_id, now=now,
                          payload={"simulation_ref": simulation.simulation_ref,
                                   "simulation_hash": simulation.simulation_hash,
                                   "projection_rev": value.projection.revision})
                decision = self._workflow._coordinator._gate.evaluate(
                    task=tx.task, manifest=manifest, snapshot=tx.snapshot, proposal=final,
                    projection=value.projection, simulation=simulation, now=now,
                )
                if decision.decision is not DecisionStatus.APPROVAL_REQUIRED:
                    raise StateConflict("COORDINATED_GATE_DENIED")
                digest = sha256(final)
                tx.write_proposal(tx.snapshot.cycles_used + 1, final, final.simulation_ref, now=now)
                tx.append(EventKind.PROPOSAL_ACCEPTED, actor=principal.actor_id, now=now,
                          payload={"proposal_hash": digest, "simulation_ref": final.simulation_ref})
                tx.append(EventKind.APPROVAL_REQUIRED, actor=principal.actor_id, now=now,
                          payload={"proposal_hash": digest})
                finish_planning_attempt(tx, value.lease, result_kind="PROPOSED", reason_code=None,
                                        proposal_hash=digest, now=now, coordinated_result_hash=record.result_hash)
            end = self._now()
            principal.require("task:prepare", principal.organization_id, end)
            principal.require("task:propose", principal.organization_id, end)
            self._source(tx, value, end)
            store._fresh_commit(locked)

    def plan(self, principal: Principal, task_id: str, body: PlanningCommand, *, now: datetime) -> PlanningReceipt:
        require_aware(now)
        actor = Principal.model_validate(principal.model_dump())
        command = PlanningCommand.model_validate(body.model_dump())
        value = self._input(actor, task_id, command)
        try:
            with self._store._repo.write_task(task_id, serialize_sqlite=True) as tx:
                checked_at = self._now()
                _task_identity(tx, actor, value.lease, value.context, checked_at)
                complete = read_consumption(tx, value) is not None
                if not complete:
                    _live_attempt(tx, value.lease, checked_at)
                    self._source(tx, value, checked_at)
            if not complete:
                # Source/permissions were rechecked on this retry, not just when
                # its input was saved. No network runs inside a SQL transaction.
                self._inference.execute(actor, value.lease, value.prompt())
            self._consume(actor, value)
        except Exception:
            # A lost consumption-commit acknowledgment is recovered by reading.
            # Never fail the lease or call execute a second time in this handler.
            with self._store._repo._sessions() as db:
                committed = db.get(ConsumptionRow, value.lease.attempt_id) is not None
            if not committed:
                raise StateConflict("COORDINATED_PLANNING_PENDING_OR_DENIED") from None
            self._consume(actor, value)
        return self._receipt(self.inspect(actor, task_id, now=self._now()))


def create_coordinated_planning(
    store: PlanningWorkspaceStore, workflow: TrustedSyntheticWorkflow, *,
    admission: TrustedInferenceAdmission, transport: CoordinatedUDSInferenceTransport,
) -> TrustedCoordinatedPlanning:
    """Trusted bootstrap for create_app(..., persistent_planning=...)."""
    return TrustedCoordinatedPlanning(store, workflow, inference=create_application_inference(
        mode="coordinated_offline", admission=admission, transport=transport,
    ))
