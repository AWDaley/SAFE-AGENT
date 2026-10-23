"""0016 migrated fencing agrees with fixtures and preserves older office evidence."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
)
from safe_agent.storage.episodes import EpisodeStore
from tests.episodes.test_episode_foundation import seed
from tests.episodes.test_episode_migration import ROOT, migrate
from tests.episodes.test_episode_model_boundary import reply, request
from tests.m7.dispatch_fixtures import binding, seed_parents


def test_0016_migration_preserves_office_rows_and_matches_runtime_constraints(tmp_path):
    path = tmp_path / "runtime_migrated.sqlite"
    migrate(path, "0014_billing_verifier_identity")
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    old_tables = inspect(engine).get_table_names()
    old_columns = {table: [str(column) for column in inspect(engine).get_columns(table)]
                   for table in old_tables}
    preserved_tables = ("safe_tasks", "safe_planning_attempts", "safe_inference_budget_reservations")
    with engine.begin() as db:
        seed_parents(db, binding())
        before = {table: db.execute(text("SELECT * FROM " + table)).all()
                  for table in preserved_tables}
    migrate(path, "0016_episode_inference")
    migrate(path, "0016_episode_inference")
    inspector = inspect(engine)
    for table in old_tables:
        assert [str(column) for column in inspector.get_columns(table)] == old_columns[table]
    with engine.connect() as db:
        for table in preserved_tables:
            assert db.execute(text("SELECT * FROM " + table)).all() == before[table]
    for model in (EpisodePrivateContextRow, EpisodeModelAttemptRow):
        table = model.__table__
        assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
        assert {constraint["name"] for constraint in inspector.get_check_constraints(table.name)} == {
            constraint.name for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert {item["name"] for item in inspector.get_indexes(table.name)} == {
            item.name for item in table.indexes
        }
    episodes = EpisodeStore(engine)
    _, _, view = seed(episodes)
    journal = EpisodeRuntimeStore(engine)
    req = request(view)
    journal.prepare_inference(req)
    journal.claim_inference(req)
    result = reply(req)
    journal.complete_inference(req, result)
    assert journal.inference(req.request_id) == (req, result, "COMPLETE")
    journal.create(view.episode_id, {"seen_observations": []})
    claim = journal.claim(view.episode_id, "observed_1", 0)
    journal.save(view.episode_id, claim, {})
    for query in (
        "UPDATE safe_episode_model_attempts SET status='PREPARED'",
        "UPDATE safe_episode_model_attempts SET request_json='{}'",
        "UPDATE safe_episode_model_attempts SET reply_hash='changed'",
        "DELETE FROM safe_episode_model_attempts",
        "UPDATE safe_episode_private_contexts SET version=0",
        "DELETE FROM safe_episode_private_contexts",
    ):
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(text(query))
    with engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016_episode_inference"
    engine.dispose()


def test_0016_postgresql_migration_renders_frozen_guard_ddl_without_connecting():
    # Rendering exercises the PostgreSQL migration branch; actual row-lock/trigger
    # execution still requires a separately available PostgreSQL acceptance DB.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade",
         "0015_benchmark_episodes:0016_episode_inference", "--sql"],
        cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
        env={**os.environ, "SAFE_AGENT_DATABASE_URL": "postgresql+psycopg://offline@localhost/offline"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "safe_guard_episode_model_attempt" in result.stdout
    assert "safe_guard_episode_private_context" in result.stdout
    assert "EPISODE_RUNTIME_HISTORY_IMMUTABLE" in result.stdout
    assert "OLD.status = 'DISPATCHING'" in result.stdout
    assert "IS DISTINCT FROM NEW.input_hash" in result.stdout
