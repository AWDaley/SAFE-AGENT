"""Frozen 0015 migration agrees with the new mappings and preserves old schemas."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from safe_agent.storage.episodes import EPISODE_TABLES, EpisodeStore
from tests.episodes.test_episode_foundation import NOW, outgoing, seed

ROOT = Path(__file__).resolve().parents[2]


def migrate(path, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision], cwd=ROOT,
        env={**os.environ, "SAFE_AGENT_DATABASE_URL": f"sqlite+pysqlite:///{path}"},
        capture_output=True, text=True, timeout=35, check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]


def test_forward_migration_matches_columns_constraints_and_retains_office_schema(tmp_path):
    path = tmp_path / "migrated.sqlite"
    migrate(path, "0014_billing_verifier_identity")
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    old_tables = inspect(engine).get_table_names()
    before = {table: inspect(engine).get_columns(table) for table in old_tables}
    migrate(path, "0015_benchmark_episodes")
    migrate(path, "0015_benchmark_episodes")
    after = inspect(engine)
    assert {table: after.get_columns(table) for table in old_tables}.keys() == before.keys()
    for table in old_tables:
        assert [str(c) for c in after.get_columns(table)] == [str(c) for c in before[table]]
    for table in EPISODE_TABLES:
        assert {column["name"] for column in after.get_columns(table.name)} == set(table.columns.keys())
        assert {item["name"] for item in after.get_check_constraints(table.name)} == {
            constraint.name for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
                for item in after.get_foreign_keys(table.name)} == {
            (tuple(element.parent.name for element in constraint.elements),
             constraint.referred_table.name,
             tuple(element.column.name for element in constraint.elements))
            for constraint in table.foreign_key_constraints
        }
    store = EpisodeStore(engine)
    _, spec, snapshot = seed(store)
    call, authority = outgoing(snapshot)
    assert store.prepare_call(spec.episode_id, call, authority, 0, NOW).emit_allowed
    assert store.replay(spec.episode_id).usage.tool_calls == 1
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(text("DELETE FROM safe_episode_outgoing_calls"))
    with engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0015_benchmark_episodes"
    engine.dispose()
