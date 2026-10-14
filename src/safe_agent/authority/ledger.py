"""Tamper-evident SQLite evidence ledger with immutable task projections.

Local-development persistence only. PostgreSQL production persistence remains an M2 gap.
No model or provider calls occur during replay.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from safe_agent.canonical import sha256
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import Task, TaskManifest, TaskState


class AuditEvent(StrictModel):
    schema_version: Literal["safe_agent.audit_event.v1"] = "safe_agent.audit_event.v1"
    task_id: Identifier
    sequence: int = Field(ge=1)
    event_type: Literal["TASK_REGISTERED", "PROPOSAL_RECORDED", "DENIED", "APPROVAL_REQUIRED",
                        "APPROVAL_GRANTED", "APPROVAL_REJECTED", "ADMITTED"]
    occurred_at: datetime
    payload_hash: Digest
    previous_hash: Digest | None
    event_hash: Digest


class AuditLedger:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("""CREATE TABLE IF NOT EXISTS manifests (
            task_id TEXT PRIMARY KEY, task_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
            manifest_hash TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS events (
            task_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL,
            PRIMARY KEY(task_id,sequence), FOREIGN KEY(task_id) REFERENCES manifests(task_id))""")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def register(self, task: Task, manifest: TaskManifest, *, now: datetime) -> AuditEvent:
        from .policy import pin_manifest
        pin_manifest(task, manifest)
        with self._db:
            self._db.execute(
                "INSERT INTO manifests(task_id,task_json,manifest_json,manifest_hash) VALUES(?,?,?,?)",
                (task.task_id,task.model_dump_json(),manifest.model_dump_json(),task.manifest_hash),
            )
            return self._append_locked(task.task_id,"TASK_REGISTERED",task.manifest_hash,now)

    def load(self, task_id: str) -> tuple[Task, TaskManifest]:
        row = self._db.execute(
            "SELECT task_json, manifest_json FROM manifests WHERE task_id=?",(task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        task, manifest = Task.model_validate_json(row[0]), TaskManifest.model_validate_json(row[1])
        from .policy import pin_manifest
        pin_manifest(task, manifest)
        return task, manifest

    def _append_locked(self, task_id: str, kind: str, payload_hash: str,
                       now: datetime) -> AuditEvent:
        now = require_aware(now)
        previous = self._db.execute(
            "SELECT event_json FROM events WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        previous_event = AuditEvent.model_validate_json(previous[0]) if previous else None
        sequence = previous_event.sequence + 1 if previous_event else 1
        predecessor = previous_event.event_hash if previous_event else None
        unsigned = dict(task_id=task_id,sequence=sequence,event_type=kind,
                        occurred_at=now,payload_hash=payload_hash,previous_hash=predecessor)
        event = AuditEvent.model_validate({**unsigned,"event_hash":sha256(unsigned)})
        self._db.execute("INSERT INTO events(task_id,sequence,event_json) VALUES(?,?,?)",
                         (task_id,sequence,event.model_dump_json()))
        return event

    def append(self, task_id: str, kind: str, payload_hash: str, *, now: datetime,
               expected_sequence: int) -> AuditEvent:
        self.load(task_id)
        with self._db:
            row = self._db.execute(
                "SELECT MAX(sequence) FROM events WHERE task_id=?",(task_id,)
            ).fetchone()
            if row[0] != expected_sequence:
                raise ValueError("stale ledger sequence")
            return self._append_locked(task_id,kind,payload_hash,now)

    def replay(self, task_id: str) -> tuple[TaskState, tuple[AuditEvent,...]]:
        self.load(task_id)
        rows=self._db.execute("SELECT event_json FROM events WHERE task_id=? ORDER BY sequence",
                              (task_id,)).fetchall()
        events=tuple(AuditEvent.model_validate_json(row[0]) for row in rows)
        state=TaskState.ASSIGNED
        predecessor=None
        for index,event in enumerate(events,1):
            fields=event.model_dump(mode="python",exclude={"event_hash", "schema_version"})
            if (event.sequence != index or event.previous_hash != predecessor
                    or event.event_hash != sha256(fields)):
                raise ValueError("tampered or missing ledger event")
            predecessor=event.event_hash
            if index==1 and event.event_type!="TASK_REGISTERED":
                raise ValueError("missing registration genesis")
            if event.event_type=="PROPOSAL_RECORDED":
                state=TaskState.PROPOSED_UNVERIFIED
            elif event.event_type=="DENIED":
                state=TaskState.REJECTED
            elif event.event_type=="APPROVAL_REQUIRED":
                state=TaskState.AWAITING_APPROVAL
            elif event.event_type=="APPROVAL_REJECTED":
                state=TaskState.REJECTED
            elif event.event_type=="ADMITTED":
                state=TaskState.PROPOSED_UNVERIFIED
        return state,events
