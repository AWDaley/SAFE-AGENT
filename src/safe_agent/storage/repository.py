"""Transactional per-task SQLAlchemy store for PostgreSQL and isolated SQLite tests.

No effect dispatcher or provider client is exposed by this repository. All writes
are trusted harness effects, not changes to business software. PostgreSQL row locks
protect concurrent proposals; SQLite tests use a single local writer.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator, TypeVar

from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import (
    Approval,
    ExecutionOutcome,
    ExecutionReceipt,
    Task,
    TaskManifest,
)
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.contracts.operator import OperatorInboxPage
from safe_agent.evidence.ledger import (
    EventKind,
    LedgerEvent,
    OfficePhase,
    TaskSnapshot,
    reduce_event,
    replay,
    seal_event,
)
from safe_agent.storage.models import (
    ApprovalRow,
    Base,
    ExecutionRow,
    GoogleTestConnectionRow,
    GoogleTestOutboxBindingRow,
    LedgerRow,
    ManifestRow,
    OfficeExecutionAuthorizationRow,
    OutboxRow,
    ProjectionRow,
    ProposalRow,
    ReferenceRow,
    SimulationRow,
    TaskRow,
)
from safe_agent.virtual_office.models import (
    CalendarProjection,
    CalendarSimulation,
    InternalTaskSimulation,
    TaskListProjection,
)

M = TypeVar("M", bound=BaseModel)


def _pack(model: BaseModel) -> dict[str, object]:
    return model.model_dump(mode="json")


def _unpack(cls: type[M], value: object) -> M:
    return cls.model_validate_json(json.dumps(value, ensure_ascii=False))


def _task_record(value: object) -> Task | InternalTaskRecord:
    if isinstance(value, dict) and value.get("schema_version") == "safe_agent.internal_task_record.v1":
        return _unpack(InternalTaskRecord, value)
    return _unpack(Task, value)


def _manifest_record(value: object) -> TaskManifest | InternalTaskManifest:
    if isinstance(value, dict) and value.get("schema_version") == "safe_agent.internal_task_manifest.v1":
        return _unpack(InternalTaskManifest, value)
    return _unpack(TaskManifest, value)


class StateConflict(ValueError):
    """Stale version or transition, never a reason to retry an external effect."""


class MissingTask(KeyError):
    pass


@dataclass(frozen=True)
class OutboxClaim:
    action_hash: str
    task_id: str
    organization_id: str
    manifest_hash: str
    proposal_hash: str
    operation: str
    source_revision: str
    source_hash: str
    attempts: int
    preceding_status: str
    worker_id: str
    provider_kind: str = "synthetic"


class TaskTransaction:
    """Unit-of-work holding a row lock for the entire state transition."""

    def __init__(self, session: Session, row: TaskRow):
        self.session = session
        self.row = row

    @property
    def snapshot(self) -> TaskSnapshot:
        return _unpack(TaskSnapshot, self.row.snapshot_json)

    @property
    def task(self) -> Task | InternalTaskRecord:
        return _task_record(self.row.task_json)

    @property
    def manifest(self) -> TaskManifest | InternalTaskManifest:
        record = self.session.get(ManifestRow, self.row.manifest_hash)
        if record is None or record.task_id != self.row.task_id:
            raise ValueError("pinned manifest missing")
        manifest = _manifest_record(record.manifest_json)
        if sha256(manifest) != record.manifest_hash:
            raise ValueError("manifest digest mismatch")
        return manifest

    def preview(self, simulation_ref: str) -> tuple[CalendarProjection, CalendarSimulation]:
        simulation_row = self.session.get(SimulationRow, simulation_ref)
        if simulation_row is None or simulation_row.task_id != self.row.task_id:
            raise StateConflict("unknown or cross-task simulation")
        projection_row = self.session.get(ProjectionRow, simulation_row.projection_rev)
        if projection_row is None or projection_row.task_id != self.row.task_id:
            raise StateConflict("projection missing or outside task")
        projection = _unpack(CalendarProjection, projection_row.projection_json)
        simulation = _unpack(CalendarSimulation, simulation_row.simulation_json)
        if (sha256(projection) != projection_row.projection_hash
                or simulation.simulation_hash != simulation_row.simulation_hash
                or simulation.projection_rev != projection.revision):
            raise StateConflict("projection/simulation persistence integrity failure")
        return projection, simulation

    def task_preview(self, simulation_ref: str) -> tuple[TaskListProjection, InternalTaskSimulation]:
        simulation_row = self.session.get(SimulationRow, simulation_ref)
        if simulation_row is None or simulation_row.task_id != self.row.task_id:
            raise StateConflict("unknown or cross-task simulation")
        projection_row = self.session.get(ProjectionRow, simulation_row.projection_rev)
        if projection_row is None or projection_row.task_id != self.row.task_id:
            raise StateConflict("task projection missing or outside task")
        projection = _unpack(TaskListProjection, projection_row.projection_json)
        simulation = _unpack(InternalTaskSimulation, simulation_row.simulation_json)
        if (sha256(projection) != projection_row.projection_hash
                or simulation.simulation_hash != simulation_row.simulation_hash
                or simulation.projection_rev != projection.revision):
            raise StateConflict("task preview persistence integrity failure")
        return projection, simulation

    def write_preview(self, projection: CalendarProjection | TaskListProjection,
                      simulation: CalendarSimulation | InternalTaskSimulation,
                      *, now: datetime) -> None:
        existing = self.session.get(SimulationRow, simulation.simulation_ref)
        if existing is not None:
            raise StateConflict("duplicate preview identifier")
        current = self.session.get(ProjectionRow, projection.revision)
        if current is not None:
            if current.task_id != self.row.task_id or current.projection_hash != sha256(projection):
                raise StateConflict("projection revision collision")
        else:
            self.session.add(ProjectionRow(
                revision=projection.revision, task_id=self.row.task_id,
                organization_id=self.row.organization_id,
                projection_hash=sha256(projection), projection_json=_pack(projection),
                recorded_at=now,
            ))
            self.session.flush()
        self.session.add(SimulationRow(
            simulation_ref=simulation.simulation_ref, task_id=self.row.task_id,
            projection_rev=projection.revision, simulation_hash=simulation.simulation_hash,
            simulation_json=_pack(simulation), registered_at=now,
        ))

    def write_proposal(self, cycle: int, proposal: BaseModel, simulation_ref: str,
                       *, now: datetime) -> None:
        self.session.add(ProposalRow(
            task_id=self.row.task_id, cycle_number=cycle,
            proposal_hash=sha256(proposal), simulation_ref=simulation_ref,
            proposal_json=_pack(proposal), recorded_at=now,
        ))

    def store_protected_references(self, entries: tuple[dict[str, object], ...]) -> None:
        """Persist trusted HMAC records in the same transaction as an action intent."""
        for entry in entries:
            if set(entry) != {"handle", "record", "mac"}:
                raise StateConflict("invalid trusted reference envelope")
            record = entry["record"]
            if not isinstance(record, dict):
                raise StateConflict("invalid trusted reference")
            if (record.get("organization_id") != self.row.organization_id
                    or record.get("task_id") != self.row.task_id
                    or record.get("initiator_id") != self.row.initiator_id):
                raise StateConflict("reference scope mismatch")
            handle = str(entry["handle"])
            previous = self.session.get(ReferenceRow, handle)
            if previous is None:
                self.session.add(ReferenceRow(
                    handle=handle, task_id=self.row.task_id,
                    organization_id=self.row.organization_id,
                    initiator_id=self.row.initiator_id,
                    kind=str(record["resource_kind"]), binding_json=entry,
                    revoked=bool(record["revoked"]),
                ))
            elif previous.binding_json != entry:
                raise StateConflict("protected reference binding was changed")
        self.session.flush()

    def write_execution_intent(
        self, action_hash: str, proposal_hash: str, *, now: datetime,
        operation: str, source_revision: str, source_hash: str,
        provider_kind: str = "synthetic",
    ) -> None:
        """One transaction: action row + leased-worker outbox + signed ledger later."""
        if self.snapshot.phase is not OfficePhase.READY_TO_COMMIT:
            raise StateConflict("execution intent requires ready task")
        if (operation not in {"calendar.create_event", "tasks.create"}
                or provider_kind not in {"synthetic", "google_test"}
                or (provider_kind == "google_test" and operation != "calendar.create_event")):
            raise StateConflict("unsupported provider/operation combination")
        effect_id = "fake_action_" + action_hash[7:39]
        if (self.session.get(ExecutionRow, effect_id) is not None
                or self.session.get(OutboxRow, action_hash) is not None):
            raise StateConflict("action intent already exists")
        self.session.add(ExecutionRow(
            effect_id=effect_id, task_id=self.row.task_id,
            proposal_hash=proposal_hash, outcome="INTENT_RECORDED",
            receipt_json={"action_hash": action_hash, "status": "INTENT_RECORDED"},
            recorded_at=now,
        ))
        self.session.add(OutboxRow(
            action_hash=action_hash, task_id=self.row.task_id,
            organization_id=self.row.organization_id,
            proposal_hash=proposal_hash, manifest_hash=self.row.manifest_hash,
            operation=operation, provider_kind=provider_kind,
            source_revision=source_revision, source_hash=source_hash,
            status="QUEUED", attempts=0,
            lease_owner=None, lease_until=None, next_attempt_at=now.astimezone(UTC),
            last_reason=None, created_at=now.astimezone(UTC),
        ))
        self.session.flush()

    def persist_office_execution_authorization(
        self, authorization: object, *, now: datetime,
    ) -> None:
        """Persist the TAL record in this outbox/ledger SQL transaction.

        This does not dispatch or enable Google writes. Only trusted code with
        the pinned upstream TAL package may invoke the operation.
        """
        from importlib import import_module

        tal_sha256 = import_module("tal.core").sha256
        OfficeExecutionAuthorization = import_module("tal.office_execution").OfficeExecutionAuthorization

        if type(authorization) is not OfficeExecutionAuthorization:
            raise StateConflict("TAL_AUTHORIZATION_TYPE_INVALID")
        raw = authorization.model_dump(mode="json")
        record = OfficeExecutionAuthorization.model_validate_json(__import__('json').dumps(raw))
        if (record != authorization or tal_sha256(
                record.model_dump(mode="python", exclude={"authorization_hash"})
        ) != record.authorization_hash):
            raise StateConflict("TAL_AUTHORIZATION_HASH_INVALID")
        task, manifest, snapshot = self.task, self.manifest, self.snapshot
        proposal = self.read_active_proposal()
        if not isinstance(task, Task) or not isinstance(manifest, TaskManifest):
            raise StateConflict("TAL_AUTHORIZATION_CALENDAR_ONLY")
        outbox = self.read_outbox(record.outbox_action_hash)
        if (not isinstance(proposal, CalendarCreateEvent)
                or snapshot.phase is not OfficePhase.EXECUTING
                or record.organization_id != task.organization_id
                or record.initiator_id != task.initiator_id
                or record.task_id != task.task_id
                or record.calendar_ref != manifest.calendar_ref
                or record.source_manifest_hash != task.manifest_hash
                or record.source_proposal_hash != sha256(proposal)
                or record.outbox_action_hash != outbox.action_hash
                or record.source_manifest_hash != outbox.manifest_hash
                or record.source_proposal_hash != outbox.proposal_hash
                or record.operation != outbox.operation
                or record.state_version != snapshot.state_version
                or record.cycles_used != snapshot.cycles_used
                or record.effects_used != snapshot.external_effects
                or record.max_cycles != manifest.budget.max_cycles
                or record.max_external_effects != manifest.budget.max_external_effects
                or record.issued_at > require_aware(now)
                or record.expires_at <= require_aware(now)):
            raise StateConflict("TAL_AUTHORIZATION_OUTBOX_BINDING_INVALID")
        if manifest.requires_approval:
            approval = self.read_approval(snapshot.approval_id) if snapshot.approval_id else None
            if (approval is None or not approval.approved
                    or record.approval_hash != tal_sha256(
                        __import__("tal.office", fromlist=["OfficeApprovalEvidence"]).OfficeApprovalEvidence(
                            approved=approval.approved, approver_id=approval.approver_id,
                            manifest_hash=approval.manifest_hash,
                            proposal_hash=approval.proposal_hash,
                            expires_at=approval.expires_at))
                    or record.approval_expires_at != approval.expires_at):
                raise StateConflict("TAL_APPROVAL_BINDING_INVALID")
        elif record.approval_hash is not None:
            raise StateConflict("TAL_UNEXPECTED_APPROVAL")
        if self.session.get(OfficeExecutionAuthorizationRow, record.outbox_action_hash):
            raise StateConflict("TAL_AUTHORIZATION_DUPLICATE")
        self.session.add(OfficeExecutionAuthorizationRow(
            action_hash=record.outbox_action_hash, task_id=task.task_id,
            organization_id=task.organization_id,
            authorization_hash=record.authorization_hash,
            authorization_json=raw, recorded_at=require_aware(now).astimezone(UTC),
        ))
        self.session.flush()

    def bind_google_test_action(
        self, *, action_hash: str, connection_ref: str,
        observation: object, request_hash: str,
        provider_simulation: object, now: datetime,
    ) -> None:
        """Pin an allowlisted, actor-owned test connection in the SAME outbox tx.

        Caller must have fetched the observation through the trusted Google
        service, which separately validates OAuth scope/calendar allowlist.
        Neither this binding nor the TAL record can dispatch a provider write.
        """
        from safe_agent.integrations.google_calendar.contracts import (
            GoogleCalendarObservation,
            GoogleTestEventIntent,
        )
        from safe_agent.integrations.google_calendar.oauth import GOOGLE_SCOPES
        from safe_agent.integrations.google_calendar.provider_simulation import (
            GoogleTestProviderSimulation,
            simulate_provider_proposal,
        )

        now = require_aware(now)
        if (type(observation) is not GoogleCalendarObservation
                or type(provider_simulation) is not GoogleTestProviderSimulation):
            raise StateConflict("GOOGLE_TRUSTED_PROVIDER_EVIDENCE_REQUIRED")
        provider_simulation.verify_hash()
        task, manifest, snapshot = self.task, self.manifest, self.snapshot
        if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
                or snapshot.phase is not OfficePhase.EXECUTING):
            raise StateConflict("GOOGLE_ACTION_NOT_EXECUTING")
        proposal = self.read_active_proposal()
        outbox = self.read_outbox(action_hash)
        auth = self.session.get(OfficeExecutionAuthorizationRow, action_hash)
        connection = self.session.get(
            GoogleTestConnectionRow, connection_ref, with_for_update=True)
        if (not isinstance(proposal, CalendarCreateEvent)
                or sha256(proposal) != outbox.proposal_hash
                or outbox.provider_kind != "google_test"
                or outbox.operation != "calendar.create_event"
                or outbox.status != "QUEUED" or outbox.attempts != 0
                or outbox.lease_owner is not None
                or auth is None or auth.task_id != task.task_id
                or auth.authorization_json.get("outbox_action_hash") != action_hash
                or auth.authorization_hash != auth.authorization_json.get("authorization_hash")
                or connection is None or connection.revoked
                or connection.organization_id != task.organization_id
                or connection.actor_id != task.initiator_id
                or set(connection.scopes.split()) != GOOGLE_SCOPES):
            raise StateConflict("GOOGLE_CONNECTION_OR_AUTHORITY_NOT_BOUND")
        if (observation.organization_id != task.organization_id
                or observation.task_id != task.task_id
                or observation.calendar_ref != manifest.calendar_ref
                or observation.connection_ref != connection_ref
                or observation.advisory_only is not True
                or now < observation.fetched_at or now >= observation.expires_at
                or observation.from_at > proposal.start_at
                or observation.until_at < proposal.end_at
                or not any(slot.start_at <= proposal.start_at and
                           proposal.end_at <= slot.end_at
                           for slot in observation.free_slots)
                or outbox.source_revision != observation.revision
                or outbox.source_hash != sha256(observation)):
            raise StateConflict("GOOGLE_OBSERVATION_NOT_BOUND")
        expected_request_hash = sha256({
            "provider": "google_test", "action_hash": action_hash,
            "manifest_hash": task.manifest_hash,
            "proposal_hash": outbox.proposal_hash,
            "connection_ref": connection_ref,
            "calendar_ref": manifest.calendar_ref,
            "start_at": proposal.start_at, "end_at": proposal.end_at,
            "time_zone": proposal.time_zone,
        })
        if request_hash != expected_request_hash:
            raise StateConflict("GOOGLE_REQUEST_HASH_MISMATCH")
        expected_simulation = simulate_provider_proposal(
            observation=observation, proposal=proposal, manifest=manifest, now=now,
        )
        if provider_simulation != expected_simulation:
            raise StateConflict("GOOGLE_PROVIDER_SIMULATION_MISMATCH")
        GoogleTestEventIntent(
            action_hash=action_hash, request_hash=request_hash,
            start_at=proposal.start_at, end_at=proposal.end_at,
            time_zone=proposal.time_zone,
        )
        if self.session.get(GoogleTestOutboxBindingRow, action_hash) is not None:
            raise StateConflict("GOOGLE_ACTION_ALREADY_BOUND")
        self.session.add(GoogleTestOutboxBindingRow(
            action_hash=action_hash, task_id=task.task_id,
            organization_id=task.organization_id,
            initiator_id=task.initiator_id, connection_id=connection_ref,
            calendar_ref=manifest.calendar_ref,
            observation_revision=observation.revision,
            observation_hash=sha256(observation),
            provider_request_hash=request_hash,
            provider_simulation_hash=expected_simulation.simulation_hash,
            provider_simulation_json=expected_simulation.model_dump(mode="json"),
            recorded_at=now.astimezone(UTC),
        ))
        self.session.flush()

    def read_outbox(self, action_hash: str) -> OutboxRow:
        record = self.session.get(OutboxRow, action_hash)
        if record is None or record.task_id != self.row.task_id:
            raise StateConflict("no outbox intent for task")
        return record

    def write_execution_outcome(self, action_hash: str, *, outcome: str,
                                receipt_json: dict[str, object]) -> None:
        effect_id = "fake_action_" + action_hash[7:39]
        record = self.session.get(ExecutionRow, effect_id)
        if (record is None or record.task_id != self.row.task_id
                or record.outcome not in {"INTENT_RECORDED", "OUTCOME_UNKNOWN"}):
            raise StateConflict("no pending execution intent")
        if outcome not in {"VERIFIED", "OUTCOME_UNKNOWN"}:
            raise StateConflict("invalid synthetic outcome")
        record.outcome = outcome
        record.receipt_json = receipt_json
        outbox = self.read_outbox(action_hash)
        if outcome == "VERIFIED":
            outbox.status = "VERIFIED"
            outbox.lease_owner = None
            outbox.lease_until = None
        elif outbox.status == "QUEUED":  # M3 legacy inline path.
            outbox.status = "NEEDS_REVIEW"
        self.session.flush()

    def mark_outbox_terminal(self, action_hash: str, *, status: str, reason: str,
                             owner: str | None = None) -> None:
        row = self.read_outbox(action_hash)
        if owner is not None and row.lease_owner != owner:
            raise StateConflict("worker no longer owns the outbox lease")
        if status not in {"NEEDS_REVIEW", "VERIFIED"}:
            raise StateConflict("invalid outbox terminal status")
        row.status = status
        row.last_reason = reason
        row.lease_owner = None
        row.lease_until = None
        self.session.flush()

    def read_approval(self, approval_id: str) -> Approval | None:
        row = self.session.get(ApprovalRow, approval_id)
        if row is None or row.task_id != self.row.task_id:
            return None
        return _unpack(Approval, row.approval_json)

    def read_active_proposal(self) -> CalendarCreateEvent | InternalTaskCreateProposal | None:
        current = self.snapshot
        row = self.session.get(ProposalRow, (self.row.task_id, current.cycles_used))
        if row is None or row.proposal_hash != current.proposal_hash:
            return None
        if row.proposal_json.get("operation") == "tasks.create":
            proposal: CalendarCreateEvent | InternalTaskCreateProposal = _unpack(
                InternalTaskCreateProposal, row.proposal_json,
            )
        else:
            proposal = _unpack(CalendarCreateEvent, row.proposal_json)
        if sha256(proposal) != row.proposal_hash:
            raise StateConflict("persisted proposal hash mismatch")
        return proposal

    def write_approval(self, approval: BaseModel, organization_id: str,
                       approver_id: str, *, now: datetime) -> None:
        values = _pack(approval)
        self.session.add(ApprovalRow(
            approval_id=str(values["approval_id"]), task_id=self.row.task_id,
            organization_id=organization_id, approver_id=approver_id,
            proposal_hash=str(values["proposal_hash"]),
            manifest_hash=str(values["manifest_hash"]),
            approval_json=values, recorded_at=now,
        ))

    def append(self, kind: EventKind, *, payload: dict[str, str], actor: str,
               now: datetime) -> LedgerEvent:
        snap = self.snapshot
        current = self.session.get(LedgerRow, (self.row.task_id, snap.ledger_sequence))
        if current is None:
            raise StateConflict("ledger head missing")
        expected_version = snap.state_version + (0 if kind is EventKind.PROPOSAL_DENIED else 1)
        event = seal_event(
            task_id=self.row.task_id, organization_id=self.row.organization_id,
            actor_id=actor, kind=kind, sequence=snap.ledger_sequence + 1,
            state_version=expected_version, now=now,
            previous_hash=current.event_hash, payload=payload,
        )
        new_state = reduce_event(snap, event)
        self.session.add(LedgerRow(
            task_id=self.row.task_id, ledger_sequence=event.ledger_sequence,
            event_id=event.event_id, organization_id=self.row.organization_id,
            event_hash=event.event_hash, previous_event_hash=event.previous_event_hash,
            event_json=_pack(event),
        ))
        self.row.snapshot_json = _pack(new_state)
        self.row.state_version = new_state.state_version
        self.row.ledger_sequence = new_state.ledger_sequence
        self.session.flush()
        return event


class LedgerRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    @classmethod
    def testing_sqlite(cls, url: str = "sqlite+pysqlite:///:memory:") -> LedgerRepository:
        if not url.startswith("sqlite"):
            raise ValueError("test create_all is restricted to SQLite")
        kwargs: dict[str, object] = {}
        if ":memory:" in url:
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        Base.metadata.create_all(engine)  # Only isolated tests; PostgreSQL uses Alembic.
        return cls(engine)

    @classmethod
    def production_engine(cls, url: str) -> LedgerRepository:
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("production repository requires PostgreSQL/psycopg")
        return cls(create_engine(url, pool_pre_ping=True))

    def create_task(self, task: Task | InternalTaskRecord,
                    manifest: TaskManifest | InternalTaskManifest, *, actor: str,
                    now: datetime) -> TaskSnapshot:
        if (task.task_id != manifest.task_id or task.organization_id != manifest.organization_id
                or task.manifest_id != manifest.manifest_id
                or task.initiator_id != manifest.initiator_id
                or task.manifest_hash != sha256(manifest)):
            raise ValueError("task is not bound to immutable manifest")
        now = require_aware(now)
        with self._sessions.begin() as session:
            if session.get(TaskRow, task.task_id) is not None:
                raise StateConflict("task already registered")
            initial = TaskSnapshot(
                task_id=task.task_id, organization_id=task.organization_id,
                initiator_id=task.initiator_id, manifest_hash=task.manifest_hash,
                ledger_sequence=1, state_version=0,
            )
            event = seal_event(
                task_id=task.task_id, organization_id=task.organization_id,
                actor_id=actor, kind=EventKind.TASK_CREATED,
                sequence=1, state_version=0, now=now,
                payload={"manifest_hash": task.manifest_hash, "initiator_id": task.initiator_id},
                previous_hash=None,
            )
            if reduce_event(None, event) != initial:
                raise AssertionError("genesis reducer diverged")
            session.add(TaskRow(
                task_id=task.task_id, organization_id=task.organization_id,
                initiator_id=task.initiator_id, manifest_id=task.manifest_id,
                manifest_hash=task.manifest_hash, task_json=_pack(task),
                snapshot_json=_pack(initial), state_version=0, ledger_sequence=1,
                created_at=task.created_at, deadline=task.deadline,
            ))
            session.flush()
            session.add(ManifestRow(
                manifest_hash=task.manifest_hash, task_id=task.task_id,
                organization_id=task.organization_id, manifest_id=manifest.manifest_id,
                manifest_version=manifest.manifest_version,
                manifest_json=_pack(manifest), registered_at=now,
            ))
            session.add(LedgerRow(
                task_id=task.task_id, ledger_sequence=1, event_id=event.event_id,
                organization_id=task.organization_id, event_hash=event.event_hash,
                previous_event_hash=None, event_json=_pack(event),
            ))
        return initial

    @contextmanager
    def write_task(self, task_id: str) -> Iterator[TaskTransaction]:
        with self._sessions.begin() as session:
            query = select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
            row = session.scalar(query)
            if row is None:
                raise MissingTask("unknown task")
            yield TaskTransaction(session, row)

    def claim_action(self, *, worker_id: str, now: datetime,
                     lease: timedelta = timedelta(seconds=30),
                     task_id: str | None = None) -> OutboxClaim | None:
        """Claim a durable command; expired leases are reconciled before any retry.

        PostgreSQL uses SELECT FOR UPDATE SKIP LOCKED. SQLite test mode uses
        a conditional UPDATE on attempts and status to reject concurrent claims.
        """
        now = require_aware(now)
        if not worker_id or not timedelta(0) < lease <= timedelta(minutes=5):
            raise ValueError("invalid worker or bounded lease")
        tick = now.astimezone(UTC)
        with self._sessions.begin() as session:
            google_binding_exists = (
                select(GoogleTestOutboxBindingRow.action_hash)
                .where(GoogleTestOutboxBindingRow.action_hash == OutboxRow.action_hash)
                .exists()
            )
            choices = [
                ~google_binding_exists,
                OutboxRow.status.in_(("QUEUED", "OUTCOME_UNKNOWN", "RETRY_WAIT", "LEASED")),
                OutboxRow.provider_kind == "synthetic",
                OutboxRow.next_attempt_at <= tick,
                or_(OutboxRow.lease_until.is_(None), OutboxRow.lease_until <= tick),
            ]
            if task_id is not None:
                choices.append(OutboxRow.task_id == task_id)
            query = (select(OutboxRow).where(*choices)
                     .order_by(OutboxRow.created_at, OutboxRow.action_hash).limit(1))
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = session.scalar(query)
            if row is None:
                return None
            previous = row.status
            attempts = row.attempts
            claimed = session.execute(update(OutboxRow).where(
                OutboxRow.action_hash == row.action_hash,
                ~google_binding_exists,
                OutboxRow.provider_kind == "synthetic",
                OutboxRow.status == previous, OutboxRow.attempts == attempts,
                OutboxRow.next_attempt_at <= tick,
                or_(OutboxRow.lease_until.is_(None), OutboxRow.lease_until <= tick),
            ).values(status="LEASED", attempts=attempts + 1,
                     lease_owner=worker_id, lease_until=tick + lease)
                .returning(OutboxRow.action_hash)
                .execution_options(synchronize_session=False))
            if claimed.scalar_one_or_none() != row.action_hash:
                return None
            return OutboxClaim(
                action_hash=row.action_hash, task_id=row.task_id,
                organization_id=row.organization_id, manifest_hash=row.manifest_hash,
                proposal_hash=row.proposal_hash, operation=row.operation,
                source_revision=row.source_revision, source_hash=row.source_hash,
                attempts=attempts + 1, preceding_status=previous, worker_id=worker_id,
                provider_kind=row.provider_kind,
            )

    def claim_google_test_action(
        self, *, worker_id: str, now: datetime,
        lease: timedelta = timedelta(seconds=30),
        task_id: str | None = None,
    ) -> OutboxClaim | None:
        """Offline Google worker lease; never accessible to synthetic dispatchers.

        Requires BOTH sealed TAL authority and the provider-specific SQL binding.
        Every lease increments attempts. A second lease is reconciliation-only,
        even after a crash before the first provider POST.
        """
        now = require_aware(now)
        if not worker_id or not timedelta(0) < lease <= timedelta(minutes=5):
            raise ValueError("invalid bounded worker lease")
        tick = now.astimezone(UTC)
        with self._sessions.begin() as session:
            has_binding = select(GoogleTestOutboxBindingRow.action_hash).where(
                GoogleTestOutboxBindingRow.action_hash == OutboxRow.action_hash
            ).exists()
            has_authority = select(OfficeExecutionAuthorizationRow.action_hash).where(
                OfficeExecutionAuthorizationRow.action_hash == OutboxRow.action_hash
            ).exists()
            constraints = [
                OutboxRow.provider_kind == "google_test",
                OutboxRow.operation == "calendar.create_event",
                OutboxRow.status.in_(("QUEUED", "OUTCOME_UNKNOWN", "LEASED")),
                has_binding, has_authority,
                OutboxRow.next_attempt_at <= tick,
                or_(OutboxRow.lease_until.is_(None), OutboxRow.lease_until <= tick),
            ]
            if task_id is not None:
                constraints.append(OutboxRow.task_id == task_id)
            query = select(OutboxRow).where(*constraints).order_by(
                OutboxRow.created_at, OutboxRow.action_hash
            ).limit(1)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = session.scalar(query)
            if row is None:
                return None
            prior, attempts = row.status, row.attempts
            updated = session.execute(update(OutboxRow).where(
                OutboxRow.action_hash == row.action_hash,
                OutboxRow.provider_kind == "google_test",
                OutboxRow.operation == "calendar.create_event",
                OutboxRow.status == prior, OutboxRow.attempts == attempts,
                has_binding, has_authority,
                OutboxRow.next_attempt_at <= tick,
                or_(OutboxRow.lease_until.is_(None), OutboxRow.lease_until <= tick),
            ).values(
                status="LEASED", attempts=attempts + 1,
                lease_owner=worker_id, lease_until=tick + lease,
            ).returning(OutboxRow.action_hash).execution_options(
                synchronize_session=False,
            ))
            if updated.scalar_one_or_none() != row.action_hash:
                return None
            return OutboxClaim(
                action_hash=row.action_hash, task_id=row.task_id,
                organization_id=row.organization_id, manifest_hash=row.manifest_hash,
                proposal_hash=row.proposal_hash, operation=row.operation,
                source_revision=row.source_revision, source_hash=row.source_hash,
                attempts=attempts + 1, preceding_status=prior,
                worker_id=worker_id, provider_kind="google_test",
            )

    def release_action_unknown(self, claim: OutboxClaim, *, now: datetime,
                               reason: str, delay: timedelta = timedelta(seconds=5)) -> None:
        """Uncertain effect remains uncompleted and must be reconciled."""
        with self.write_task(claim.task_id) as tx:
            row = tx.read_outbox(claim.action_hash)
            if row.status != "LEASED" or row.lease_owner != claim.worker_id:
                raise StateConflict("worker no longer owns outbox claim")
            receipt = ExecutionReceipt(
                action_hash=claim.action_hash, manifest_hash=claim.manifest_hash,
                outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
            )
            tx.write_execution_outcome(
                claim.action_hash, outcome="OUTCOME_UNKNOWN",
                receipt_json=receipt.model_dump(mode="json"),
            )
            row.status = "OUTCOME_UNKNOWN"
            row.last_reason = reason
            row.next_attempt_at = now.astimezone(UTC) + delay
            row.lease_owner = None
            row.lease_until = None
            tx.session.flush()

    def read_outbox(self, task_id: str) -> dict[str, object]:
        """Trusted operator/recovery summary without private resource bindings."""
        with self._sessions() as session:
            row = session.scalar(select(OutboxRow).where(OutboxRow.task_id == task_id))
            if row is None:
                raise MissingTask("no durable action for task")
            return {
                "action_hash": row.action_hash, "task_id": row.task_id,
                "status": row.status, "provider_kind": row.provider_kind,
                "attempts": row.attempts,
                "last_reason": row.last_reason,
            }

    def read_office_execution_authorization(
        self, task_id: str, action_hash: str,
    ) -> object:
        """Trusted protected lookup, not a model-visible or bearer permission."""
        from importlib import import_module

        tal_sha256 = import_module("tal.core").sha256
        OfficeExecutionAuthorization = import_module("tal.office_execution").OfficeExecutionAuthorization

        with self._sessions.begin() as session:
            task = session.get(TaskRow, task_id)
            outbox = session.get(OutboxRow, action_hash)
            row = session.get(OfficeExecutionAuthorizationRow, action_hash)
            if (task is None or outbox is None or row is None
                    or row.task_id != task_id or outbox.task_id != task_id
                    or row.organization_id != task.organization_id
                    or outbox.organization_id != task.organization_id
                    or task.manifest_hash != outbox.manifest_hash):
                raise StateConflict("TAL_AUTHORIZATION_NOT_FOUND")
            record = OfficeExecutionAuthorization.model_validate_json(json.dumps(row.authorization_json))
            if (row.authorization_hash != record.authorization_hash
                    or tal_sha256(record.model_dump(mode="python", exclude={
                        "authorization_hash"})) != record.authorization_hash
                    or record.outbox_action_hash != action_hash
                    or record.task_id != task_id
                    or record.organization_id != outbox.organization_id
                    or record.source_manifest_hash != outbox.manifest_hash
                    or record.source_proposal_hash != outbox.proposal_hash
                    or record.operation != outbox.operation):
                raise StateConflict("TAL_AUTHORIZATION_TAMPERED")
            return record

    def read_google_test_action_binding(
        self, task_id: str, action_hash: str,
    ) -> dict[str, str]:
        """Private, integrity-checked connection/outbox metadata for future worker.

        Never expose this method in an operator/agent API. No OAuth credentials,
        raw provider IDs or HTTP calls are returned.
        """
        from safe_agent.integrations.google_calendar.provider_simulation import (
            GoogleProviderSimulationInvalid,
            GoogleTestProviderSimulation,
        )

        with self._sessions.begin() as session:
            task = session.get(TaskRow, task_id)
            outbox = session.get(OutboxRow, action_hash)
            row = session.get(GoogleTestOutboxBindingRow, action_hash)
            authority = session.get(OfficeExecutionAuthorizationRow, action_hash)
            if (task is None or outbox is None or row is None or authority is None
                    or outbox.task_id != task_id or row.task_id != task_id
                    or authority.task_id != task_id
                    or row.organization_id != task.organization_id
                    or row.initiator_id != task.initiator_id
                    or outbox.organization_id != task.organization_id
                    or outbox.provider_kind != "google_test"
                    or outbox.operation != "calendar.create_event"
                    or outbox.proposal_hash != authority.authorization_json.get("source_proposal_hash")
                    or row.observation_revision != outbox.source_revision
                    or row.observation_hash != outbox.source_hash
                    or row.calendar_ref != authority.authorization_json.get("calendar_ref")
                    or authority.authorization_hash != authority.authorization_json.get("authorization_hash")):
                raise StateConflict("GOOGLE_OUTBOX_BINDING_INTEGRITY_ERROR")
            snap = _unpack(TaskSnapshot, task.snapshot_json)
            proposal_row = session.get(ProposalRow, (task_id, snap.cycles_used))
            if proposal_row is None:
                raise StateConflict("GOOGLE_BOUND_PROPOSAL_NOT_FOUND")
            proposal = _unpack(CalendarCreateEvent, proposal_row.proposal_json)
            if (sha256(proposal) != proposal_row.proposal_hash
                    or proposal_row.proposal_hash != outbox.proposal_hash):
                raise StateConflict("GOOGLE_BOUND_PROPOSAL_TAMPERED")
            expected_request_hash = sha256({
                "provider": "google_test", "action_hash": action_hash,
                "manifest_hash": outbox.manifest_hash,
                "proposal_hash": outbox.proposal_hash,
                "connection_ref": row.connection_id,
                "calendar_ref": row.calendar_ref,
                "start_at": proposal.start_at, "end_at": proposal.end_at,
                "time_zone": proposal.time_zone,
            })
            if row.provider_request_hash != expected_request_hash:
                raise StateConflict("GOOGLE_OUTBOX_REQUEST_HASH_TAMPERED")
            try:
                if row.provider_simulation_json is None or row.provider_simulation_hash is None:
                    raise GoogleProviderSimulationInvalid("PROVIDER_SIMULATION_NOT_RECORDED")
                simulation = GoogleTestProviderSimulation.model_validate_json(
                    json.dumps(row.provider_simulation_json),
                )
                simulation.verify_hash()
                if (simulation.simulation_hash != row.provider_simulation_hash
                        or simulation.provenance != "google_test_calendar"
                        or simulation.observation_revision != outbox.source_revision
                        or simulation.observation_hash != outbox.source_hash
                        or simulation.proposal_hash != outbox.proposal_hash
                        or simulation.organization_id != row.organization_id
                        or simulation.task_id != task_id
                        or simulation.calendar_ref != row.calendar_ref
                        or simulation.connection_ref != row.connection_id
                        or simulation.start_at != proposal.start_at
                        or simulation.end_at != proposal.end_at):
                    raise GoogleProviderSimulationInvalid("GOOGLE_PROVIDER_SIMULATION_MISMATCH")
            except (ValueError, TypeError) as error:
                raise StateConflict("GOOGLE_PROVIDER_SIMULATION_INTEGRITY_ERROR") from error
            connection = session.get(GoogleTestConnectionRow, row.connection_id)
            if (connection is None or connection.revoked
                    or connection.organization_id != row.organization_id
                    or connection.actor_id != row.initiator_id):
                raise StateConflict("GOOGLE_BOUND_CONNECTION_UNAVAILABLE")
            return {
                "action_hash": row.action_hash,
                "task_id": row.task_id,
                "organization_id": row.organization_id,
                "initiator_id": row.initiator_id,
                "connection_ref": row.connection_id,
                "calendar_ref": row.calendar_ref,
                "observation_revision": row.observation_revision,
                "observation_hash": row.observation_hash,
                "provider_request_hash": row.provider_request_hash,
                "provider_simulation_hash": row.provider_simulation_hash,
                "authorization_hash": authority.authorization_hash,
                "provider_kind": outbox.provider_kind,
            }

    def list_operator_tasks(
        self, organization_id: str, *, after: str | None = None, limit: int = 20,
    ) -> OperatorInboxPage:
        """Bounded, strictly tenant-filtered task snapshots; no private bindings.

        Lexical task-ID cursor is stable across inserts and never names a
        cross-tenant record. Caller must enforce authenticated task:read.
        """
        from safe_agent.contracts.operator import OperatorInboxItem

        if not 1 <= limit <= 50:
            raise ValueError("operator inbox page must contain 1 to 50 entries")
        query = select(TaskRow).where(TaskRow.organization_id == organization_id)
        if after is not None:
            query = query.where(TaskRow.task_id > after)
        query = query.order_by(TaskRow.task_id).limit(limit + 1)
        with self._sessions() as session:
            rows = session.scalars(query).all()
            visible = rows[:limit]
            items: list[OperatorInboxItem] = []
            for row in visible:
                snapshot = _unpack(TaskSnapshot, row.snapshot_json)
                if (snapshot.task_id != row.task_id
                        or snapshot.organization_id != organization_id
                        or snapshot.state_version != row.state_version
                        or snapshot.ledger_sequence != row.ledger_sequence):
                    raise StateConflict("operator inbox snapshot integrity failed")
                items.append(OperatorInboxItem(
                    task_id=snapshot.task_id, phase=snapshot.phase,
                    state_version=snapshot.state_version,
                    has_proposal=snapshot.proposal_hash is not None,
                    approval_pending=snapshot.phase is OfficePhase.AWAITING_APPROVAL,
                ))
            return OperatorInboxPage(
                items=tuple(items),
                next_cursor=visible[-1].task_id if len(rows) > limit else None,
            )

    def read_task(self, task_id: str) -> TaskSnapshot:
        with self._sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise MissingTask("unknown task")
            return _unpack(TaskSnapshot, row.snapshot_json)

    def read_task_record(self, task_id: str) -> Task | InternalTaskRecord:
        with self._sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise MissingTask("unknown task")
            return _task_record(row.task_json)

    def read_events(self, task_id: str) -> tuple[LedgerEvent, ...]:
        with self._sessions() as session:
            exists = session.get(TaskRow, task_id)
            if exists is None:
                raise MissingTask("unknown task")
            rows = session.scalars(
                select(LedgerRow).where(LedgerRow.task_id == task_id).order_by(
                    LedgerRow.ledger_sequence)
            ).all()
            return tuple(_unpack(LedgerEvent, row.event_json) for row in rows)

    def replay_task(self, task_id: str) -> TaskSnapshot:
        persisted = self.read_task(task_id)
        events = self.read_events(task_id)
        # Verify columns separately, not only the JSON blob.
        with self._sessions() as session:
            rows = session.scalars(select(LedgerRow).where(LedgerRow.task_id == task_id).order_by(
                LedgerRow.ledger_sequence)).all()
            for row, event in zip(rows, events, strict=True):
                if (row.event_hash != event.event_hash
                        or row.previous_event_hash != event.previous_event_hash
                        or row.ledger_sequence != event.ledger_sequence):
                    raise ValueError("ledger indexing/hash columns are inconsistent")
            task = session.get(TaskRow, task_id)
            if task is None or task.state_version != persisted.state_version or (
                task.ledger_sequence != persisted.ledger_sequence
            ):
                raise ValueError("projection metadata mismatch")
        rebuilt = replay(events)
        if rebuilt != persisted:
            raise ValueError("replay projection diverged from durable state")
        # The pure reducer proves ledger order and event contents. This
        # additional check binds trusted execution receipts to the final
        # evidence chain without querying an external provider during replay.
        with self._sessions() as session:
            effects = session.scalars(select(ExecutionRow).where(
                ExecutionRow.task_id == task_id)).all()
            intent = next((e for e in events if e.kind is EventKind.ACTION_INTENT_RECORDED), None)
            if persisted.phase in {OfficePhase.EXECUTING, OfficePhase.COMPLETED,
                                   OfficePhase.NEEDS_REVIEW}:
                if len(effects) != 1 or intent is None:
                    raise ValueError("effect intent/evidence is missing")
                effect = effects[0]
                expected_effect_id = "fake_action_" + intent.payload["action_hash"][7:39]
                if (effect.effect_id != expected_effect_id
                        or effect.proposal_hash != intent.payload["proposal_hash"]
                        or effect.proposal_hash != persisted.proposal_hash):
                    raise ValueError("execution intent binding mismatch")
                if persisted.phase is OfficePhase.EXECUTING:
                    if effect.outcome not in {"INTENT_RECORDED", "OUTCOME_UNKNOWN"}:
                        raise ValueError("pending intent outcome mismatch")
                    if effect.outcome == "OUTCOME_UNKNOWN":
                        waiting = _unpack(ExecutionReceipt, effect.receipt_json)
                        if (waiting.action_hash != intent.payload["action_hash"]
                                or waiting.manifest_hash != persisted.manifest_hash
                                or waiting.outcome is not ExecutionOutcome.OUTCOME_UNKNOWN):
                            raise ValueError("unknown effect receipt integrity failure")
                else:
                    receipt = _unpack(ExecutionReceipt, effect.receipt_json)
                    if (receipt.action_hash != intent.payload["action_hash"]
                            or receipt.manifest_hash != persisted.manifest_hash):
                        raise ValueError("execution receipt is not bound to intent")
                    if persisted.phase is OfficePhase.COMPLETED:
                        verified = events[-1]
                        if (effect.outcome != "VERIFIED"
                                or receipt.outcome is not ExecutionOutcome.COMMITTED
                                or verified.kind is not EventKind.ACTION_VERIFIED
                                or verified.payload["effect_ref"] != receipt.effect_ref
                                or receipt.effect_ref != persisted.effect_ref
                                or receipt.verification_ref != (
                                    "verification_" + verified.payload["verification_hash"][7:39]
                                ) or persisted.verification_hash != verified.payload["verification_hash"]):
                            raise ValueError("verified receipt/evidence mismatch")
                    elif (effect.outcome != "OUTCOME_UNKNOWN"
                          or receipt.outcome is not ExecutionOutcome.OUTCOME_UNKNOWN
                          or events[-1].kind is not EventKind.ACTION_UNVERIFIED):
                        raise ValueError("uncertain outcome evidence mismatch")
            elif effects:
                raise ValueError("execution record present before dispatch intent")
        with self._sessions() as session:
            outbox = session.scalar(select(OutboxRow).where(OutboxRow.task_id == task_id))
            if persisted.phase in {OfficePhase.EXECUTING, OfficePhase.COMPLETED,
                                   OfficePhase.NEEDS_REVIEW}:
                if outbox is None or intent is None or outbox.manifest_hash != persisted.manifest_hash:
                    raise ValueError("outbox missing or manifest changed")
                if (outbox.action_hash != intent.payload["action_hash"]
                        or outbox.proposal_hash != persisted.proposal_hash):
                    raise ValueError("outbox not bound to ledger intent")
                if outbox.provider_kind == "google_test":
                    from safe_agent.integrations.google_calendar.provider_simulation import (
                        GoogleTestProviderSimulation,
                    )
                    binding = session.get(GoogleTestOutboxBindingRow, outbox.action_hash)
                    auth = session.get(
                        OfficeExecutionAuthorizationRow, outbox.action_hash)
                    if (binding is None or auth is None
                            or binding.task_id != task_id
                            or binding.organization_id != persisted.organization_id
                            or binding.initiator_id != persisted.initiator_id
                            or binding.observation_revision != outbox.source_revision
                            or binding.observation_hash != outbox.source_hash
                            or auth.task_id != task_id
                            or auth.authorization_json.get("outbox_action_hash") != outbox.action_hash
                            or auth.authorization_hash != auth.authorization_json.get(
                                "authorization_hash")
                            or binding.provider_simulation_json is None
                            or binding.provider_simulation_hash is None):
                        raise ValueError("Google outbox authorization/binding replay mismatch")
                    try:
                        sim = GoogleTestProviderSimulation.model_validate_json(
                            json.dumps(binding.provider_simulation_json),
                        )
                        sim.verify_hash()
                        if (sim.simulation_hash != binding.provider_simulation_hash
                                or sim.observation_revision != outbox.source_revision
                                or sim.observation_hash != outbox.source_hash
                                or sim.proposal_hash != outbox.proposal_hash
                                or sim.calendar_ref != binding.calendar_ref
                                or sim.connection_ref != binding.connection_id
                                or sim.task_id != task_id):
                            raise ValueError("provider simulation binding mismatch")
                    except (ValueError, TypeError) as error:
                        raise ValueError("Google provider simulation replay mismatch") from error
                elif outbox.provider_kind != "synthetic":
                    raise ValueError("unsupported outbox provider kind")
                if (persisted.phase is OfficePhase.COMPLETED and outbox.status != "VERIFIED"):
                    raise ValueError("completed action outbox not verified")
                if (persisted.phase is OfficePhase.NEEDS_REVIEW
                        and outbox.status != "NEEDS_REVIEW"):
                    raise ValueError("escalated outbox not marked for review")
            elif outbox is not None:
                raise ValueError("outbox exists before authority intent")
        return rebuilt

    def close(self) -> None:
        self.engine.dispose()

    def persist_protected_references(self, task_id: str, organization_id: str,
                                     initiator_id: str, records: tuple[dict[str, object], ...]) -> None:
        """Store HMAC-protected private mappings. NEVER return these via operator APIs."""
        with self.write_task(task_id) as tx:
            if tx.row.organization_id != organization_id or tx.row.initiator_id != initiator_id:
                raise StateConflict("reference principal/tenant mismatch")
            for entry in records:
                if set(entry) != {"handle", "record", "mac"}:
                    raise StateConflict("invalid protected reference envelope")
                binding = entry["record"]
                if not isinstance(binding, dict):
                    raise StateConflict("invalid protected reference body")
                if (binding.get("organization_id") != organization_id
                        or binding.get("initiator_id") != initiator_id
                        or binding.get("task_id") != task_id):
                    raise StateConflict("cross-organization reference rejected")
                handle = str(entry["handle"])
                current = tx.session.get(ReferenceRow, handle)
                if current is None:
                    tx.session.add(ReferenceRow(
                        handle=handle, task_id=task_id, organization_id=organization_id,
                        initiator_id=initiator_id, kind=str(binding["resource_kind"]),
                        binding_json=entry, revoked=bool(binding["revoked"]),
                    ))
                elif current.binding_json != entry:
                    # Only explicit revocation through the trusted broker may alter it.
                    if (current.binding_json.get("record", {}).get("revoked") is False
                            and binding.get("revoked") is True):
                        current.revoked = True
                        current.binding_json = entry
                    else:
                        raise StateConflict("reference binding is immutable")

    def load_protected_references(self, task_id: str, organization_id: str) -> tuple[dict[str, object], ...]:
        with self._sessions() as session:
            task = session.get(TaskRow, task_id)
            if task is None or task.organization_id != organization_id:
                raise MissingTask("unknown task")
            rows = session.scalars(select(ReferenceRow).where(
                ReferenceRow.task_id == task_id).order_by(ReferenceRow.handle)).all()
            return tuple(row.binding_json for row in rows)
