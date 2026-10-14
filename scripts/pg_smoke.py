"""M2 transient-PostgreSQL migration + write-once evidence smoke test."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.storage.repository import LedgerRepository


def main() -> None:
    url = os.environ["SAFE_AGENT_DATABASE_URL"]
    repo = LedgerRepository.production_engine(url)
    fixture = json.loads((Path(__file__).resolve().parents[1]
                         / "fixtures/synthetic_office/demo.json").read_text())
    task = Task.model_validate_json(json.dumps(fixture["task"]))
    manifest = TaskManifest.model_validate_json(json.dumps(fixture["manifest"]))
    repo.create_task(task, manifest, actor="employee_demo",
                     now=datetime.fromisoformat("2026-10-14T13:00:00-04:00"))
    assert repo.replay_task(task.task_id).ledger_sequence == 1
    try:
        with repo.engine.begin() as conn:
            conn.execute(text("UPDATE safe_evidence_ledger SET event_hash=event_hash "
                              "WHERE task_id=:id"), {"id": task.task_id})
    except DBAPIError:
        pass
    else:
        raise AssertionError("PostgreSQL ledger immutability trigger is not enforcing")
    assert repo.replay_task(task.task_id).ledger_sequence == 1
    with repo.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM safe_execution_records")) == 0
    repo.close()
    print("PostgreSQL migration, replay, and immutable ledger trigger: PASS")


if __name__ == "__main__":
    main()
