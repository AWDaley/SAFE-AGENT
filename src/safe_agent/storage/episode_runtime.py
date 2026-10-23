"""Private episode context and durable model-attempt fencing.

These tables contain native customer data. They are host storage, never model
inputs or public evidence. Lost processing leases fail closed and need explicit
reconciliation; expiration does not permit replay of an uncertain dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import (
    DDL,
    CheckConstraint,
    CursorResult,
    Integer,
    String,
    Table,
    Text,
    event,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from safe_agent.contracts.episode_inference import (
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    strict_json,
    wire_digest,
    wire_json,
)
from safe_agent.storage.models import Base


class EpisodePrivateContextRow(Base):
    __tablename__ = "safe_episode_private_contexts"
    episode_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    active_observation: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (CheckConstraint("version >= 0", name="ck_episode_context_version"),)


class EpisodeModelAttemptRow(Base):
    __tablename__ = "safe_episode_model_attempts"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reply_json: Mapped[str | None] = mapped_column(Text)
    reply_hash: Mapped[str | None] = mapped_column(String(72))
    __table_args__ = (
        CheckConstraint("status IN ('PREPARED', 'DISPATCHING', 'COMPLETE', 'UNCERTAIN')",
                        name="ck_episode_model_status"),
        CheckConstraint(
            "(status = 'COMPLETE' AND reply_json IS NOT NULL AND reply_hash IS NOT NULL) OR "
            "(status != 'COMPLETE' AND reply_json IS NULL AND reply_hash IS NULL)",
            name="ck_episode_model_reply_state",
        ),
    )


MODEL_INSERT_DENIED = """
NEW.status != 'PREPARED' OR NEW.reply_json IS NOT NULL OR NEW.reply_hash IS NOT NULL
OR CASE WHEN json_valid(NEW.request_json) THEN
    json_type(NEW.request_json) != 'object'
    OR json_extract(NEW.request_json, '$.request_id') IS NOT NEW.request_id
    OR json_extract(NEW.request_json, '$.episode_id') IS NOT NEW.episode_id
    OR json_extract(NEW.request_json, '$.model') IS NULL
ELSE 1 END
"""
MODEL_UPDATE_DENIED = """
NEW.request_id != OLD.request_id OR NEW.episode_id != OLD.episode_id
OR NEW.request_json != OLD.request_json OR NEW.input_hash != OLD.input_hash
OR NOT (
    (OLD.status = 'PREPARED' AND NEW.status = 'DISPATCHING')
    OR (OLD.status = 'DISPATCHING' AND NEW.status IN ('COMPLETE', 'UNCERTAIN'))
)
OR (NEW.status = 'COMPLETE' AND CASE WHEN json_valid(NEW.reply_json) THEN
    json_type(NEW.reply_json) != 'object'
    OR json_extract(NEW.reply_json, '$.request_id') IS NOT NEW.request_id
    OR json_extract(NEW.reply_json, '$.input_hash') IS NOT NEW.input_hash
    OR json_extract(NEW.reply_json, '$.model') IS NOT json_extract(NEW.request_json, '$.model')
ELSE 1 END)
"""
CONTEXT_INSERT_DENIED = "NEW.version != 0 OR NEW.active_observation IS NOT NULL"
CONTEXT_UPDATE_DENIED = """
NEW.episode_id != OLD.episode_id OR NEW.version != OLD.version + 1
OR NOT (
    (OLD.active_observation IS NULL AND NEW.active_observation IS NOT NULL
     AND NEW.context_json = OLD.context_json AND NEW.context_hash = OLD.context_hash)
    OR (OLD.active_observation IS NOT NULL AND NEW.active_observation IS NULL)
)
"""

# Match migration 0016 for isolated SQLite fixtures. Actual PostgreSQL acceptance
# must use the frozen migration, not create_all as a substitute.
for _mapping, _prefix, _insert, _update in (
    (EpisodeModelAttemptRow, "episode_model", MODEL_INSERT_DENIED, MODEL_UPDATE_DENIED),
    (EpisodePrivateContextRow, "episode_context", CONTEXT_INSERT_DENIED, CONTEXT_UPDATE_DENIED),
):
    _table = cast(Table, _mapping.__table__)
    for _operation, _guard in (("INSERT", _insert), ("UPDATE", _update)):
        event.listen(_table, "after_create", DDL(
            f"CREATE TRIGGER safe_{_prefix}_{_operation.lower()}_guard "
            f"BEFORE {_operation} ON {_table.name} WHEN {_guard} "
            "BEGIN SELECT RAISE(ABORT, 'EPISODE_RUNTIME_FENCE_DENIED'); END"
        ).execute_if(dialect="sqlite"))
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER safe_{_prefix}_no_delete BEFORE DELETE ON {_table.name} "
        "BEGIN SELECT RAISE(ABORT, 'EPISODE_RUNTIME_HISTORY_IMMUTABLE'); END"
    ).execute_if(dialect="sqlite"))


class RuntimeStorageDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextSnapshot:
    version: int
    data: dict[str, Any]
    active_observation: str | None


class EpisodeRuntimeStore:
    def __init__(self, engine: Engine):
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def create(self, episode_id: str, data: dict[str, Any]) -> None:
        with self._sessions.begin() as session:
            if session.get(EpisodePrivateContextRow, episode_id) is not None:
                raise RuntimeStorageDenied("EPISODE_ALREADY_EXISTS")
            session.add(EpisodePrivateContextRow(
                episode_id=episode_id, version=0, context_json=wire_json(data),
                context_hash=wire_digest(data), active_observation=None,
            ))

    @staticmethod
    def _context(row: EpisodePrivateContextRow) -> ContextSnapshot:
        try:
            data = strict_json(row.context_json, max_bytes=32 * 1024 * 1024)
            if not isinstance(data, dict) or wire_digest(data) != row.context_hash:
                raise ValueError("invalid context binding")
        except (ValueError, TypeError, RecursionError):
            raise RuntimeStorageDenied("CONTEXT_INTEGRITY_FAILED") from None
        return ContextSnapshot(row.version, data, row.active_observation)

    def load(self, episode_id: str) -> ContextSnapshot:
        with self._sessions() as session:
            row = session.get(EpisodePrivateContextRow, episode_id)
            if row is None:
                raise RuntimeStorageDenied("UNKNOWN_EPISODE")
            return self._context(row)

    def claim(self, episode_id: str, observation_id: str, expected_version: int) -> ContextSnapshot:
        with self._sessions.begin() as session:
            row = session.get(EpisodePrivateContextRow, episode_id)
            if row is None:
                raise RuntimeStorageDenied("UNKNOWN_EPISODE")
            before = self._context(row)
            if observation_id in before.data.get("seen_observations", []):
                raise RuntimeStorageDenied("OBSERVATION_ALREADY_CONSUMED")
            changed = session.execute(update(EpisodePrivateContextRow).where(
                EpisodePrivateContextRow.episode_id == episode_id,
                EpisodePrivateContextRow.version == expected_version,
                EpisodePrivateContextRow.active_observation.is_(None),
            ).values(active_observation=observation_id, version=expected_version + 1))
            if cast(CursorResult[Any], changed).rowcount != 1:
                raise RuntimeStorageDenied("EPISODE_PROCESSING_OR_STALE")
            return ContextSnapshot(expected_version + 1, before.data, observation_id)

    def save(self, episode_id: str, snapshot: ContextSnapshot, data: dict[str, Any]) -> None:
        if snapshot.active_observation is None:
            raise RuntimeStorageDenied("CONTEXT_REQUIRES_PROCESSING_CLAIM")
        seen = data.get("seen_observations", [])
        if not isinstance(seen, list) or not all(isinstance(item, str) for item in seen):
            raise RuntimeStorageDenied("INVALID_OBSERVATION_HISTORY")
        with self._sessions.begin() as session:
            row = session.get(EpisodePrivateContextRow, episode_id)
            if row is None:
                raise RuntimeStorageDenied("UNKNOWN_EPISODE")
            current = self._context(row)
            old_seen = current.data.get("seen_observations", [])
            if not isinstance(old_seen, list) or not all(isinstance(item, str) for item in old_seen):
                raise RuntimeStorageDenied("INVALID_OBSERVATION_HISTORY")
            # A caller cannot erase earlier consumed observations by saving a
            # fresh context dictionary. The database version CAS covers races.
            data = {**data, "seen_observations": list(dict.fromkeys([
                *old_seen, *seen, snapshot.active_observation,
            ]))}
            changed = session.execute(update(EpisodePrivateContextRow).where(
                EpisodePrivateContextRow.episode_id == episode_id,
                EpisodePrivateContextRow.version == snapshot.version,
                EpisodePrivateContextRow.active_observation == snapshot.active_observation,
            ).values(context_json=wire_json(data), context_hash=wire_digest(data),
                     active_observation=None, version=snapshot.version + 1))
            if cast(CursorResult[Any], changed).rowcount != 1:
                raise RuntimeStorageDenied("CONTEXT_VERSION_CHANGED")

    def prepare_inference(self, request: EpisodeInferenceRequest) -> None:
        with self._sessions.begin() as session:
            if session.get(EpisodeModelAttemptRow, request.request_id) is not None:
                raise RuntimeStorageDenied("INFERENCE_ALREADY_EXISTS")
            session.add(EpisodeModelAttemptRow(
                request_id=request.request_id, episode_id=request.episode_id,
                request_json=request.model_dump_json(), input_hash=request.digest,
                status="PREPARED",
            ))

    def claim_inference(self, request: EpisodeInferenceRequest) -> None:
        with self._sessions.begin() as session:
            changed = session.execute(update(EpisodeModelAttemptRow).where(
                EpisodeModelAttemptRow.request_id == request.request_id,
                EpisodeModelAttemptRow.episode_id == request.episode_id,
                EpisodeModelAttemptRow.input_hash == request.digest,
                EpisodeModelAttemptRow.status == "PREPARED",
            ).values(status="DISPATCHING"))
            if cast(CursorResult[Any], changed).rowcount != 1:
                raise RuntimeStorageDenied("INFERENCE_ALREADY_DISPATCHED")

    def complete_inference(self, request: EpisodeInferenceRequest, reply: EpisodeInferenceReply) -> None:
        if (reply.request_id != request.request_id or reply.input_hash != request.digest
                or reply.model != request.model):
            raise RuntimeStorageDenied("INFERENCE_RESULT_MISMATCH")
        with self._sessions.begin() as session:
            changed = session.execute(update(EpisodeModelAttemptRow).where(
                EpisodeModelAttemptRow.request_id == request.request_id,
                EpisodeModelAttemptRow.input_hash == request.digest,
                EpisodeModelAttemptRow.status == "DISPATCHING",
            ).values(status="COMPLETE", reply_json=reply.model_dump_json(), reply_hash=reply.digest))
            if cast(CursorResult[Any], changed).rowcount != 1:
                raise RuntimeStorageDenied("INFERENCE_RESULT_REPLAY")

    def uncertain(self, request_id: str) -> None:
        with self._sessions.begin() as session:
            session.execute(update(EpisodeModelAttemptRow).where(
                EpisodeModelAttemptRow.request_id == request_id,
                EpisodeModelAttemptRow.status == "DISPATCHING",
            ).values(status="UNCERTAIN"))

    def inference(self, request_id: str) -> tuple[EpisodeInferenceRequest, EpisodeInferenceReply | None, str]:
        with self._sessions() as session:
            row = session.scalar(select(EpisodeModelAttemptRow).where(
                EpisodeModelAttemptRow.request_id == request_id))
            if row is None:
                raise RuntimeStorageDenied("UNKNOWN_INFERENCE")
            try:
                request = EpisodeInferenceRequest.model_validate_json(row.request_json)
                reply = EpisodeInferenceReply.model_validate_json(row.reply_json) if row.reply_json else None
            except (ValueError, TypeError):
                raise RuntimeStorageDenied("INFERENCE_STORAGE_INTEGRITY_FAILED") from None
            if (request.request_id != row.request_id or request.episode_id != row.episode_id
                    or request.digest != row.input_hash
                    or row.status not in {"PREPARED", "DISPATCHING", "COMPLETE", "UNCERTAIN"}
                    or (row.status == "COMPLETE") != (reply is not None)
                    or (reply is None and row.reply_hash is not None)
                    or (reply and (
                        reply.digest != row.reply_hash or reply.request_id != request.request_id
                        or reply.input_hash != request.digest or reply.model != request.model
                    ))):
                raise RuntimeStorageDenied("INFERENCE_STORAGE_INTEGRITY_FAILED")
            return request, reply, row.status
