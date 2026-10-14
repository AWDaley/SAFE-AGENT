"""Transactional per-task SQLAlchemy store for PostgreSQL and isolated SQLite tests.

No effect dispatcher or provider client is exposed by this repository. All writes
are trusted harness effects, not changes to business software. PostgreSQL row locks
protect concurrent proposals; SQLite tests use a single local writer.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, TypeVar

from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select
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
    LedgerRow,
    ManifestRow,
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

    def write_execution_intent(self, action_hash: str, proposal_hash: str,
                               *, now: datetime) -> None:
        if self.snapshot.phase.value != "READY_TO_COMMIT":
            raise StateConflict("execution intent requires ready task")
        effect_id = "fake_action_" + action_hash[7:39]
        if self.session.get(ExecutionRow, effect_id) is not None:
            raise StateConflict("action intent already exists")
        self.session.add(ExecutionRow(
            effect_id=effect_id, task_id=self.row.task_id,
            proposal_hash=proposal_hash, outcome="INTENT_RECORDED",
            receipt_json={"action_hash": action_hash, "status": "INTENT_RECORDED"},
            recorded_at=now,
        ))
        self.session.flush()

    def write_execution_outcome(self, action_hash: str, *, outcome: str,
                                receipt_json: dict[str, object]) -> None:
        effect_id = "fake_action_" + action_hash[7:39]
        record = self.session.get(ExecutionRow, effect_id)
        if (record is None or record.task_id != self.row.task_id
                or record.outcome != "INTENT_RECORDED"):
            raise StateConflict("no pending execution intent")
        if outcome not in {"VERIFIED", "OUTCOME_UNKNOWN"}:
            raise StateConflict("invalid synthetic outcome")
        record.outcome = outcome
        record.receipt_json = receipt_json
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
                    if effect.outcome != "INTENT_RECORDED":
                        raise ValueError("pending intent outcome mismatch")
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
