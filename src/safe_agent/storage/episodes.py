"""Durable episode journal and exact outgoing-call outbox.

This trusted-host repository never calls a model or a business tool.  An adapter
may emit only the first ``prepare_call`` receipt with ``emit_allowed=True``.
Recovery returns non-emittable receipts; an unknown result never authorizes a
retry. SQLite takes a writer lock before reads; PostgreSQL locks run then episode.
Aggregate reservations are conservative lifetime charges, including uncertainty.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, cast

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    CursorResult,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    select,
    text,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.episodes import (
    AuthorizationBinding,
    BudgetDelta,
    EpisodeEvent,
    EpisodeEventKind,
    EpisodeSpec,
    EpisodeState,
    EpisodeView,
    InferenceReservation,
    OutgoingCall,
    PreparedCall,
    RunSpec,
    RunView,
    ToolResult,
)
from safe_agent.evidence.episodes import (
    EpisodeConflict,
    add_budget,
    reduce_episode_event,
    replay_episode,
    require_budget,
    seal_episode_event,
)
from safe_agent.storage.models import Base


class EpisodeRunRow(Base):
    __tablename__ = "safe_episode_runs"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    __table_args__ = (
        CheckConstraint("state_version >= 0 AND turns >= 0 AND model_calls >= 0 "
                        "AND tool_calls >= 0 AND tokens >= 0 AND cost_microusd >= 0",
                        name="ck_episode_run_budget"),
    )


class EpisodeRow(Base):
    __tablename__ = "safe_episodes"
    episode_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_episode_runs.run_id"), nullable=False
    )
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint("state_version >= 0", name="ck_episode_version"),)


class EpisodeEventRow(Base):
    __tablename__ = "safe_episode_events"
    episode_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_episodes.episode_id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    event_hash: Mapped[str] = mapped_column(String(71), unique=True, nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_episode_event_sequence"),
        UniqueConstraint("episode_id", "kind", "external_id", name="uq_episode_event_identity"),
    )


class EpisodeInferenceRow(Base):
    __tablename__ = "safe_episode_inference_reservations"
    episode_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_episodes.episode_id"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True, unique=True)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    record_hash: Mapped[str] = mapped_column(String(71), nullable=False)


class EpisodeInferenceResultRow(Base):
    __tablename__ = "safe_episode_inference_results"
    episode_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    __table_args__ = (ForeignKeyConstraint(
        ["episode_id", "request_id"],
        ["safe_episode_inference_reservations.episode_id",
         "safe_episode_inference_reservations.request_id"],
    ),)


class EpisodeCallRow(Base):
    __tablename__ = "safe_episode_outgoing_calls"
    episode_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_episodes.episode_id"), primary_key=True
    )
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    record_hash: Mapped[str] = mapped_column(String(71), nullable=False)


class EpisodeCallResultRow(Base):
    __tablename__ = "safe_episode_tool_results"
    episode_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    __table_args__ = (ForeignKeyConstraint(
        ["episode_id", "call_id"],
        ["safe_episode_outgoing_calls.episode_id", "safe_episode_outgoing_calls.call_id"],
    ),)


EPISODE_TABLES: tuple[Table, ...] = tuple(cast(Table, mapping.__table__) for mapping in (
    EpisodeRunRow, EpisodeRow, EpisodeEventRow, EpisodeInferenceRow,
    EpisodeInferenceResultRow, EpisodeCallRow, EpisodeCallResultRow,
))
IMMUTABLE_TABLES = EPISODE_TABLES[2:]

# Unit tests using create_all receive the same SQLite evidence guards as migration
# 0015. PostgreSQL installations must use migrations, not create_all acceptance.
for _table in IMMUTABLE_TABLES:
    if not isinstance(_table, Table):
        raise TypeError("episode evidence requires concrete SQL tables")
    for _operation in ("UPDATE", "DELETE"):
        event.listen(_table, "after_create", DDL(
            f"CREATE TRIGGER {_table.name}_no_{_operation.lower()} "
            f"BEFORE {_operation} ON {_table.name} "
            "BEGIN SELECT RAISE(ABORT, 'EPISODE_EVIDENCE_IMMUTABLE'); END"
        ).execute_if(dialect="sqlite"))

RUN_UPDATE_DENIED = (
    "NEW.run_id != OLD.run_id OR NEW.spec_json != OLD.spec_json "
    "OR NEW.spec_hash != OLD.spec_hash OR NEW.state_version != OLD.state_version + 1 "
    "OR NEW.turns < OLD.turns OR NEW.model_calls < OLD.model_calls "
    "OR NEW.tool_calls < OLD.tool_calls OR NEW.tokens < OLD.tokens "
    "OR NEW.cost_microusd < OLD.cost_microusd"
)
EPISODE_UPDATE_DENIED = (
    "NEW.episode_id != OLD.episode_id OR NEW.run_id != OLD.run_id "
    "OR NEW.spec_json != OLD.spec_json OR NEW.spec_hash != OLD.spec_hash "
    "OR NEW.state_version != OLD.state_version + 1"
)
for _table, _guard in (
    (EPISODE_TABLES[0], RUN_UPDATE_DENIED),
    (EPISODE_TABLES[1], EPISODE_UPDATE_DENIED),
):
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER {_table.name}_update_guard BEFORE UPDATE ON {_table.name} "
        f"WHEN {_guard} BEGIN SELECT RAISE(ABORT, 'EPISODE_IDENTITY_OR_BUDGET_DENIED'); END"
    ).execute_if(dialect="sqlite"))
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER {_table.name}_no_delete BEFORE DELETE ON {_table.name} "
        "BEGIN SELECT RAISE(ABORT, 'EPISODE_HISTORY_IMMUTABLE'); END"
    ).execute_if(dialect="sqlite"))


def _sqlite_foreign_keys(connection: Any, _: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _json(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


class EpisodeStore:
    """Trusted transactional store, scoped by episode and durable aggregate run."""

    def __init__(self, engine: Engine):
        if engine.dialect.name not in {"sqlite", "postgresql"}:
            raise ValueError("episode storage supports only SQLite and PostgreSQL")
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        if engine.dialect.name == "sqlite":
            if not event.contains(engine, "connect", _sqlite_foreign_keys):
                event.listen(engine, "connect", _sqlite_foreign_keys)
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def create_schema(self) -> None:
        """Isolated SQLite fixtures only; production schema uses migration 0015."""
        if self.engine.dialect.name != "sqlite":
            raise ValueError("PostgreSQL episode schema requires reviewed migrations")
        Base.metadata.create_all(self.engine, tables=list(EPISODE_TABLES))

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self.sessions() as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                else:
                    session.begin()
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _run_view(row: EpisodeRunRow) -> RunView:
        spec = RunSpec.model_validate_json(row.spec_json)
        if spec.run_id != row.run_id or sha256(spec) != row.spec_hash:
            raise EpisodeConflict("run specification integrity failure")
        usage = BudgetDelta(**{name: getattr(row, name) for name in BudgetDelta.model_fields})
        require_budget(usage, spec.limits)
        return RunView(spec=spec, usage=usage, state_version=row.state_version)

    @staticmethod
    def _view(row: EpisodeRow) -> EpisodeView:
        spec = EpisodeSpec.model_validate_json(row.spec_json)
        snapshot = EpisodeView.model_validate_json(row.snapshot_json)
        if (sha256(spec) != row.spec_hash or spec.episode_id != row.episode_id
                or spec.run_id != row.run_id or snapshot.state_version != row.state_version
                or any(getattr(snapshot, key) != getattr(spec, key) for key in (
                    "episode_id", "run_id", "actor_id", "domain", "policy_hash",
                    "limits", "created_at", "deadline",
                ))):
            raise EpisodeConflict("episode snapshot integrity failure")
        require_budget(snapshot.usage, spec.limits)
        return snapshot

    def _locked(self, session: Session, episode_id: str) -> tuple[EpisodeRunRow, EpisodeRow]:
        run_id = session.scalar(select(EpisodeRow.run_id).where(EpisodeRow.episode_id == episode_id))
        if run_id is None:
            raise KeyError("unknown episode")
        run = session.scalar(select(EpisodeRunRow).where(
            EpisodeRunRow.run_id == run_id
        ).with_for_update().execution_options(populate_existing=True))
        episode = session.scalar(select(EpisodeRow).where(
            EpisodeRow.episode_id == episode_id
        ).with_for_update().execution_options(populate_existing=True))
        if run is None or episode is None:
            raise EpisodeConflict("episode run binding missing")
        self._run_view(run)
        self._view(episode)
        return run, episode

    @staticmethod
    def _new_work(run: EpisodeRunRow, snapshot: EpisodeView, now: datetime) -> None:
        require_aware(now)
        run_spec = RunSpec.model_validate_json(run.spec_json)
        if now < snapshot.updated_at or now >= min(run_spec.deadline, snapshot.deadline):
            raise EpisodeConflict("new work outside episode/run time bounds")

    @staticmethod
    def _version(snapshot: EpisodeView, expected_version: int | None) -> None:
        if expected_version is not None and snapshot.state_version != expected_version:
            raise EpisodeConflict("stale episode state version")

    def _charge(self, session: Session, run: EpisodeRunRow, delta: BudgetDelta) -> None:
        if not any(getattr(delta, name) for name in BudgetDelta.model_fields):
            return
        current = self._run_view(run)
        updated = add_budget(current.usage, delta)
        require_budget(updated, current.spec.limits)
        result = session.execute(update(EpisodeRunRow).where(
            EpisodeRunRow.run_id == run.run_id,
            EpisodeRunRow.state_version == current.state_version,
        ).values(
            **updated.model_dump(), state_version=current.state_version + 1
        ).execution_options(synchronize_session=False))
        if cast(CursorResult[Any], result).rowcount != 1:
            raise EpisodeConflict("run budget compare-and-swap failed")

    def _advance(
        self, session: Session, run: EpisodeRunRow, row: EpisodeRow,
        kind: EpisodeEventKind, payload: dict[str, str], now: datetime,
        *, external_id: str | None = None,
    ) -> EpisodeView:
        old = self._view(row)
        record = seal_episode_event(
            episode_id=row.episode_id, kind=kind, sequence=old.ledger_sequence + 1,
            state_version=old.state_version + 1, now=now, payload=payload,
            previous_hash=old.last_event_hash,
        )
        snapshot = reduce_episode_event(old, record)
        delta = BudgetDelta(**{
            name: getattr(snapshot.usage, name) - getattr(old.usage, name)
            for name in BudgetDelta.model_fields
        })
        self._charge(session, run, delta)
        result = session.execute(update(EpisodeRow).where(
            EpisodeRow.episode_id == row.episode_id, EpisodeRow.state_version == old.state_version,
        ).values(
            snapshot_json=_json(snapshot), state_version=snapshot.state_version
        ).execution_options(synchronize_session=False))
        if cast(CursorResult[Any], result).rowcount != 1:
            raise EpisodeConflict("episode state compare-and-swap failed")
        session.add(EpisodeEventRow(
            episode_id=row.episode_id, sequence=record.sequence, kind=kind.value,
            external_id=external_id, event_hash=record.event_hash, record_json=_json(record),
        ))
        session.flush()
        return snapshot

    @staticmethod
    def _prior_event(
        session: Session, episode_id: str, kind: EpisodeEventKind, external_id: str,
    ) -> EpisodeEvent | None:
        row = session.scalar(select(EpisodeEventRow).where(
            EpisodeEventRow.episode_id == episode_id,
            EpisodeEventRow.kind == kind.value, EpisodeEventRow.external_id == external_id,
        ))
        if row is None:
            return None
        record = EpisodeEvent.model_validate_json(row.record_json)
        if record.event_hash != row.event_hash:
            raise EpisodeConflict("event row integrity failure")
        return record

    def create_run(self, spec: RunSpec) -> RunView:
        with self._transaction() as session:
            row = session.scalar(select(EpisodeRunRow).where(
                EpisodeRunRow.run_id == spec.run_id
            ).with_for_update())
            if row is not None:
                view = self._run_view(row)
                if view.spec != spec:
                    raise EpisodeConflict("run ID already binds a different specification")
                return view
            row = EpisodeRunRow(
                run_id=spec.run_id, spec_json=_json(spec), spec_hash=sha256(spec),
                state_version=0, **BudgetDelta().model_dump(),
            )
            session.add(row)
            session.flush()
            return self._run_view(row)

    def create_episode(self, spec: EpisodeSpec) -> EpisodeView:
        with self._transaction() as session:
            run = session.scalar(select(EpisodeRunRow).where(
                EpisodeRunRow.run_id == spec.run_id
            ).with_for_update())
            if run is None:
                raise KeyError("unknown episode run")
            run_view = self._run_view(run)
            row = session.get(EpisodeRow, spec.episode_id)
            if row is not None:
                if EpisodeSpec.model_validate_json(row.spec_json) != spec:
                    raise EpisodeConflict("episode ID already binds a different specification")
                return self._view(row)
            # Run policy binds the frozen evaluation configuration; episode policy
            # independently binds this domain's actual policy and tool schemas.
            if (spec.created_at < run_view.spec.created_at
                    or spec.deadline > run_view.spec.deadline):
                raise EpisodeConflict("episode does not bind run time scope")
            for field in type(spec.limits).model_fields:
                if getattr(spec.limits, field) > getattr(run_view.spec.limits, field):
                    raise EpisodeConflict("episode limit exceeds run allowance")
            record = seal_episode_event(
                episode_id=spec.episode_id, kind=EpisodeEventKind.CREATED, sequence=1,
                state_version=0, now=spec.created_at, payload={"spec_json": _json(spec)},
                previous_hash=None,
            )
            snapshot = reduce_episode_event(None, record)
            session.add(EpisodeRow(
                episode_id=spec.episode_id, run_id=spec.run_id, spec_json=_json(spec),
                spec_hash=sha256(spec), state_version=0, snapshot_json=_json(snapshot),
            ))
            session.flush()
            session.add(EpisodeEventRow(
                episode_id=spec.episode_id, sequence=1, kind=record.kind.value,
                event_hash=record.event_hash, record_json=_json(record),
            ))
            return snapshot

    def get_run(self, run_id: str) -> RunView:
        with self.sessions() as session:
            row = session.get(EpisodeRunRow, run_id)
            if row is None:
                raise KeyError("unknown episode run")
            return self._run_view(row)

    def inspect(self, episode_id: str) -> EpisodeView:
        with self.sessions() as session:
            row = session.get(EpisodeRow, episode_id)
            if row is None:
                raise KeyError("unknown episode")
            return self._view(row)

    get_episode = inspect

    def events(self, episode_id: str) -> tuple[EpisodeEvent, ...]:
        with self.sessions() as session:
            if session.get(EpisodeRow, episode_id) is None:
                raise KeyError("unknown episode")
            rows = session.scalars(select(EpisodeEventRow).where(
                EpisodeEventRow.episode_id == episode_id
            ).order_by(EpisodeEventRow.sequence))
            events = tuple(EpisodeEvent.model_validate_json(row.record_json) for row in rows)
            return events

    def replay(self, episode_id: str) -> EpisodeView:
        # One consistent read transaction avoids comparing different live versions.
        with self._transaction() as session:
            _, row = self._locked(session, episode_id)
            records = session.scalars(select(EpisodeEventRow).where(
                EpisodeEventRow.episode_id == episode_id
            ).order_by(EpisodeEventRow.sequence))
            replayed = replay_episode(
                EpisodeEvent.model_validate_json(record.record_json) for record in records
            )
            if replayed != self._view(row):
                raise EpisodeConflict("episode journal and materialized state diverge")
            return replayed

    def record_user_turn(
        self, episode_id: str, message_id: str, content_hash: str, now: datetime,
        *, expected_version: int | None = None,
    ) -> EpisodeView:
        payload = {"message_id": message_id, "content_hash": content_hash}
        return self._message(
            episode_id, EpisodeEventKind.USER_TURN, message_id, payload, now, expected_version
        )

    def record_response(
        self, episode_id: str, message_id: str, content_hash: str,
        awaiting_customer: bool, now: datetime, *, expected_version: int | None = None,
    ) -> EpisodeView:
        if type(awaiting_customer) is not bool:
            raise EpisodeConflict("response disposition must be a boolean")
        payload = {
            "message_id": message_id, "content_hash": content_hash,
            "awaiting_customer": str(awaiting_customer).lower(),
        }
        return self._message(
            episode_id, EpisodeEventKind.RESPONSE, message_id, payload, now, expected_version
        )

    def _message(
        self, episode_id: str, kind: EpisodeEventKind, message_id: str,
        payload: dict[str, str], now: datetime, expected_version: int | None,
    ) -> EpisodeView:
        # Reuse the correlation contract without ever persisting plaintext messages.
        ToolResult(call_id=message_id, result_hash=payload["content_hash"], success=True)
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            snapshot = self._view(row)
            prior = self._prior_event(session, episode_id, kind, message_id)
            if prior is not None:
                if prior.payload != payload:
                    raise EpisodeConflict("message ID replay changed content or disposition")
                return snapshot
            self._version(snapshot, expected_version)
            self._new_work(run, snapshot, now)
            return self._advance(session, run, row, kind, payload, now, external_id=message_id)

    def record_observation(
        self, episode_id: str, observation_hash: str, expected_version: int, now: datetime,
    ) -> EpisodeView:
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            snapshot = self._view(row)
            self._version(snapshot, expected_version)
            self._new_work(run, snapshot, now)
            if snapshot.observation_hash == observation_hash:
                return snapshot
            return self._advance(
                session, run, row, EpisodeEventKind.OBSERVATION,
                {"observation_hash": observation_hash}, now,
            )

    def reserve_inference(
        self, episode_id: str, request_id: str, input_hash: str,
        budget_delta: BudgetDelta, now: datetime, *, expected_version: int | None = None,
    ) -> InferenceReservation:
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            snapshot = self._view(row)
            prior = session.scalar(select(EpisodeInferenceRow).where(
                EpisodeInferenceRow.request_id == request_id
            ))
            if prior is not None:
                reservation = self._reservation(session, prior)
                if (reservation.episode_id != episode_id or reservation.input_hash != input_hash
                        or reservation.budget != budget_delta):
                    raise EpisodeConflict("inference request ID already binds a different input")
                return reservation
            self._version(snapshot, expected_version)
            self._new_work(run, snapshot, now)
            reservation = InferenceReservation(
                episode_id=episode_id, run_id=row.run_id, request_id=request_id,
                input_hash=input_hash, budget=budget_delta,
                state_version=snapshot.state_version + 1, created_at=now,
            )
            self._advance(session, run, row, EpisodeEventKind.INFERENCE_RESERVED, {
                "request_id": request_id, "input_hash": input_hash,
                "budget_json": _json(budget_delta),
            }, now, external_id=request_id)
            session.add(EpisodeInferenceRow(
                episode_id=episode_id, request_id=request_id,
                record_json=_json(reservation), record_hash=sha256(reservation),
            ))
            return reservation

    def _reservation(self, session: Session, row: EpisodeInferenceRow) -> InferenceReservation:
        result = InferenceReservation.model_validate_json(row.record_json)
        if (sha256(result) != row.record_hash or result.episode_id != row.episode_id
                or result.request_id != row.request_id or result.result_hash is not None
                or result.uncertain):
            raise EpisodeConflict("inference reservation integrity failure")
        completion = session.get(EpisodeInferenceResultRow, (row.episode_id, row.request_id))
        uncertain = self._prior_event(
            session, row.episode_id, EpisodeEventKind.INFERENCE_UNCERTAIN, row.request_id
        )
        return result.model_copy(update={
            "result_hash": completion.result_hash if completion else None,
            "uncertain": uncertain is not None and completion is None,
        })

    def get_reservation(self, episode_id: str, request_id: str) -> InferenceReservation:
        with self.sessions() as session:
            row = session.get(EpisodeInferenceRow, (episode_id, request_id))
            if row is None:
                raise KeyError("unknown episode inference reservation")
            return self._reservation(session, row)

    def finish_inference(
        self, episode_id: str, request_id: str, result_hash: str, now: datetime,
    ) -> EpisodeView:
        ToolResult(call_id=request_id, result_hash=result_hash, success=True)
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            reservation = session.get(EpisodeInferenceRow, (episode_id, request_id))
            if reservation is None:
                raise EpisodeConflict("inference result has no matching reservation")
            self._reservation(session, reservation)
            prior = session.get(EpisodeInferenceResultRow, (episode_id, request_id))
            if prior is not None:
                if prior.result_hash != result_hash:
                    raise EpisodeConflict("inference result replay changed content")
                return self._view(row)
            snapshot = self._advance(session, run, row, EpisodeEventKind.INFERENCE_FINISHED, {
                "request_id": request_id, "result_hash": result_hash,
            }, now, external_id=request_id)
            session.add(EpisodeInferenceResultRow(
                episode_id=episode_id, request_id=request_id, result_hash=result_hash,
            ))
            return snapshot

    def prepare_call(
        self, episode_id: str, call: OutgoingCall, authorization: AuthorizationBinding,
        expected_version: int, now: datetime,
    ) -> PreparedCall:
        if type(call) is not OutgoingCall or type(authorization) is not AuthorizationBinding:
            raise EpisodeConflict("outgoing calls require exact host contract types")
        # Pydantic model_copy/model_construct can bypass validation. Reparse at the
        # durable authority boundary before emitting even a trusted host receipt.
        call = OutgoingCall.model_validate_json(call.model_dump_json())
        authorization = AuthorizationBinding.model_validate_json(authorization.model_dump_json())
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            snapshot = self._view(row)
            prior = session.get(EpisodeCallRow, (episode_id, call.call_id))
            if prior is not None:
                receipt = self._call(session, prior)
                if (receipt.call != call or receipt.authorization != authorization
                        or expected_version != authorization.state_version):
                    raise EpisodeConflict("call ID replay changed exact outgoing call or authority")
                return receipt
            self._version(snapshot, expected_version)
            self._new_work(run, snapshot, now)
            if (authorization.episode_id != episode_id
                    or authorization.state_version != expected_version
                    or authorization.action_hash != call.action_hash
                    or authorization.policy_hash != snapshot.policy_hash
                    or call.policy_hash != snapshot.policy_hash
                    or authorization.observation_hash != snapshot.observation_hash
                    or call.observation_hash != snapshot.observation_hash
                    or authorization.consent_hash != call.consent_hash
                    or authorization.allowed is not True):
                raise EpisodeConflict("outgoing call does not bind current host authorization")
            updated = self._advance(session, run, row, EpisodeEventKind.CALL_PREPARED, {
                "call_id": call.call_id, "action_hash": call.action_hash,
                "observation_hash": call.observation_hash,
                "authorization_hash": sha256(authorization),
            }, now, external_id=call.call_id)
            persisted = PreparedCall(
                episode_id=episode_id, call=call, authorization=authorization,
                prepared_at=now, state_version=updated.state_version, emit_allowed=False,
            )
            session.add(EpisodeCallRow(
                episode_id=episode_id, call_id=call.call_id, action_hash=call.action_hash,
                # Preserve exact native Unicode bytes inside arguments_json. SAFE
                # canonicalization normalizes strings; the separately bound byte
                # digest protects them but must not rewrite the persisted call.
                record_json=persisted.model_dump_json(exclude_none=True),
                record_hash=sha256(persisted),
            ))
            # The transaction commits on leaving the context before this receipt is returned.
            return persisted.model_copy(update={"emit_allowed": True})

    def _call(self, session: Session, row: EpisodeCallRow) -> PreparedCall:
        receipt = PreparedCall.model_validate_json(row.record_json)
        if (sha256(receipt) != row.record_hash or receipt.episode_id != row.episode_id
                or receipt.call.call_id != row.call_id or receipt.call.action_hash != row.action_hash
                or receipt.emit_allowed or receipt.result_hash is not None
                or receipt.success is not None or receipt.uncertain):
            raise EpisodeConflict("outgoing call persistence integrity failure")
        completion = session.get(EpisodeCallResultRow, (row.episode_id, row.call_id))
        uncertain = self._prior_event(
            session, row.episode_id, EpisodeEventKind.CALL_UNCERTAIN, row.call_id
        )
        return receipt.model_copy(update={
            "result_hash": completion.result_hash if completion else None,
            "success": completion.success if completion else None,
            "uncertain": uncertain is not None and completion is None,
        })

    def get_call(self, episode_id: str, call_id: str) -> PreparedCall:
        with self.sessions() as session:
            row = session.get(EpisodeCallRow, (episode_id, call_id))
            if row is None:
                raise KeyError("unknown episode outgoing call")
            return self._call(session, row)

    def consume_result(
        self, episode_id: str, call_id: str, result_hash: str, success: bool, now: datetime,
    ) -> EpisodeView:
        result = ToolResult(call_id=call_id, result_hash=result_hash, success=success)
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            outgoing = session.get(EpisodeCallRow, (episode_id, call_id))
            if outgoing is None:
                raise EpisodeConflict("tool result has no matching durable outgoing call")
            self._call(session, outgoing)
            prior = session.get(EpisodeCallResultRow, (episode_id, call_id))
            if prior is not None:
                if prior.result_hash != result.result_hash or prior.success != result.success:
                    raise EpisodeConflict("tool result replay changed content or success")
                return self._view(row)
            updated = self._advance(session, run, row, EpisodeEventKind.CALL_RESULT, {
                "call_id": call_id, "result_hash": result_hash, "success": str(success).lower(),
            }, now, external_id=call_id)
            session.add(EpisodeCallResultRow(
                episode_id=episode_id, call_id=call_id, result_hash=result_hash, success=success,
            ))
            return updated

    def _uncertain(
        self, episode_id: str, correlation_id: str, reason_hash: str,
        kind: EpisodeEventKind, now: datetime,
    ) -> EpisodeView:
        ToolResult(call_id=correlation_id, result_hash=reason_hash, success=False)
        key = "call_id" if kind is EpisodeEventKind.CALL_UNCERTAIN else "request_id"
        payload = {key: correlation_id, "reason_hash": reason_hash}
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            prior = self._prior_event(session, episode_id, kind, correlation_id)
            if prior is not None:
                if prior.payload != payload:
                    raise EpisodeConflict("uncertainty replay changed evidence")
                return self._view(row)
            return self._advance(
                session, run, row, kind, payload, now, external_id=correlation_id
            )

    def mark_call_uncertain(
        self, episode_id: str, call_id: str, reason_hash: str, now: datetime,
    ) -> EpisodeView:
        return self._uncertain(
            episode_id, call_id, reason_hash, EpisodeEventKind.CALL_UNCERTAIN, now
        )

    def mark_inference_uncertain(
        self, episode_id: str, request_id: str, reason_hash: str, now: datetime,
    ) -> EpisodeView:
        return self._uncertain(
            episode_id, request_id, reason_hash, EpisodeEventKind.INFERENCE_UNCERTAIN, now
        )

    def finish(
        self, episode_id: str, disposition: EpisodeState | str, now: datetime,
        *, expected_version: int | None = None,
    ) -> EpisodeView:
        disposition = EpisodeState(disposition)
        with self._transaction() as session:
            run, row = self._locked(session, episode_id)
            snapshot = self._view(row)
            if snapshot.state is disposition and disposition in {
                EpisodeState.FINISHED, EpisodeState.HANDOFF, EpisodeState.FAILED,
            }:
                return snapshot
            self._version(snapshot, expected_version)
            return self._advance(session, run, row, EpisodeEventKind.FINISHED, {
                "disposition": disposition.value,
            }, now, external_id="terminal")
