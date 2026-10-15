"""Real migrated SQLite constraints, forward lineage and ORM/schema parity."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from safe_agent.contracts.inference_dispatch import seal_invocation_event
from safe_agent.storage.models import (
    Base,
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
    InferenceInvocationRow,
)
from tests.m7.dispatch_fixtures import NOW, binding, initial_values, seed_parents

ROOT = Path(__file__).resolve().parents[2]
INVOCATIONS = InferenceInvocationRow.__table__
EVENTS = InferenceInvocationEventRow.__table__


def migrate(path: Path, revision="head") -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision], cwd=ROOT,
        env={**os.environ, "SAFE_AGENT_DATABASE_URL": f"sqlite+pysqlite:///{path}"},
        capture_output=True, text=True, timeout=35, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def sqlite_engine(path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("invocation_schema") / "template.sqlite"
    migrate(path, "0010_inference_dispatch")  # Keep the original foundation acceptance pinned.
    return path


@pytest.fixture
def database(tmp_path, migrated_template):
    path = tmp_path / "case.sqlite"
    shutil.copyfile(migrated_template, path)
    engine = sqlite_engine(path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_0010_schema_matches_orm_columns_foreign_keys_unique_and_check_constraints(database):
    actual = inspect(database)
    for table in (INVOCATIONS, EVENTS):
        assert {c["name"] for c in actual.get_columns(table.name)} == set(table.columns.keys())
        actual_checks = {c["name"] for c in actual.get_check_constraints(table.name)}
        assert actual_checks == {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
        actual_fks = {
            (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
            for f in actual.get_foreign_keys(table.name)
        }
        expected_fks = {
            (tuple(c.parent.name for c in f.elements), f.referred_table.name,
             tuple(c.column.name for c in f.elements))
            for f in table.foreign_key_constraints
        }
        assert actual_fks == expected_fks
        assert {tuple(c["column_names"]) for c in actual.get_unique_constraints(table.name)} == {
            tuple(c.name for c in u.columns) for u in table.constraints
            if u.__class__.__name__ == "UniqueConstraint"
        }
    with database.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
            "0010_inference_dispatch",
        ]


def test_one_invocation_per_attempt_and_no_orphan_or_cross_bound_event(database):
    a = binding()
    b = binding("b", task_key="a", attempt_key="a")
    with database.begin() as conn:
        seed_parents(conn, a)
        seed_parents(conn, b, task=False, attempt=False)
        conn.execute(INVOCATIONS.insert().values(**initial_values(a)))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(INVOCATIONS.insert().values(**initial_values(b)))
        first = seal_invocation_event(a, state="RESERVED", occurred_at=NOW)
        values = first.model_dump(exclude={"schema_version"})
        conn.execute(EVENTS.insert().values(**values))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(EVENTS.insert().values(**values))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(EVENTS.insert().values(**{**values, "request_id": b.request_id}))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(EVENTS.insert().values(**{
                **values, "sequence": 2, "binding_hash": b.binding_hash,
                "previous_state": "RESERVED", "state": "DISPATCH_STARTED",
                "preceding_hash": first.event_hash,
            }))
        assert conn.execute(select(INVOCATIONS.c.request_id)).scalars().all() == [a.request_id]
        assert conn.execute(select(InferenceBudgetReservationRow.held_microusd)).scalars().all() == [10, 10]


@pytest.mark.parametrize("field,value", [
    ("organization_id", "org_other"), ("task_id", "task_other"),
    ("attempt_id", "attempt_other"), ("manifest_hash", "sha256:" + "0" * 64),
    ("route_id", "different_route"), ("model_revision", "different_model"),
    ("prompt_digest", "sha256:" + "0" * 64), ("task_state_version", 1),
    ("lease_expires_at", NOW + timedelta(seconds=89)),
    ("deadline_at", NOW + timedelta(seconds=9)),
])
def test_mismatched_tenant_task_attempt_manifest_or_reservation_is_rejected(database, field, value):
    a = binding()
    with database.begin() as conn:
        seed_parents(conn, a)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(INVOCATIONS.insert().values(**{**initial_values(a), field: value}))
        assert conn.execute(select(INVOCATIONS.c.request_id)).first() is None


@pytest.mark.parametrize("changes", [
    {"state": "COMPLETED"}, {"state": "RESPONSE_VALIDATED"},
    {"state": "UNKNOWN", "reason_code": "DISPATCH_OUTCOME_UNKNOWN"},
    {"state_version": -1}, {"claim_version": -1}, {"workspace_version": 0},
    {"event_sequence": 0}, {"state_version": 1},
    {"claim_owner": "worker_a"}, {"claim_until": NOW + timedelta(seconds=1)},
    {"claim_owner": "worker_a", "claim_until": NOW + timedelta(seconds=11), "claim_version": 1},
    {"proposal_hash": "sha256:" + "0" * 64}, {"reason_code": "RAW_SECRET"},
    {"dispatch_started_at": NOW}, {"updated_at": NOW - timedelta(seconds=1)},
])
def test_invalid_lifecycle_claims_and_false_completion_are_rejected(database, changes):
    a = binding()
    with database.begin() as conn:
        seed_parents(conn, a)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(INVOCATIONS.insert().values(**{**initial_values(a), **changes}))


def test_missing_budget_reservation_prevents_invocation_even_for_zero_cost(database):
    a = binding()
    with database.begin() as conn:
        seed_parents(conn, a, reservation=False)
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(INVOCATIONS.insert().values(**initial_values(a)))


def test_upgrade_from_existing_0009_preserves_cost_holds_and_history(tmp_path):
    path = tmp_path / "upgrade.sqlite"
    migrate(path, "0009_inference_cost_accounts")
    engine = sqlite_engine(path)
    with engine.begin() as conn:
        seed_parents(conn, binding())
        before = dict(conn.execute(select(InferenceBudgetReservationRow.__table__)).mappings().one())
    engine.dispose()
    migrate(path)
    engine = sqlite_engine(path)
    try:
        with engine.begin() as conn:
            after = dict(conn.execute(select(InferenceBudgetReservationRow.__table__)).mappings().one())
            assert before == after
            conn.execute(INVOCATIONS.insert().values(**initial_values(binding())))
    finally:
        engine.dispose()


@pytest.mark.parametrize("change", ["rewrite_0009", "competing_0009", "wrong_0010_parent"])
def test_lineage_gate_fails_on_rewrite_fork_or_wrong_ancestor(tmp_path, monkeypatch, change):
    from scripts import check_m7_budget_lineage as gate

    shutil.copyfile(ROOT / "alembic.ini", tmp_path / "alembic.ini")
    shutil.copytree(ROOT / "migrations", tmp_path / "migrations",
                    ignore=shutil.ignore_patterns("__pycache__"))
    versions = tmp_path / "migrations" / "versions"
    if change == "rewrite_0009":
        original = versions / "0009_inference_cost_accounts.py"
        original.write_text(original.read_text() + "\n# unreviewed historical rewrite\n")
    elif change == "competing_0009":
        (versions / "0009_inference_budget.py").write_text(
            'revision = "0009_inference_budget"\ndown_revision = "0008_planning_workspace"\n'
        )
    else:
        migration = versions / "0010_inference_dispatch.py"
        migration.write_text(migration.read_text().replace(
            'down_revision = "0009_inference_cost_accounts"',
            'down_revision = "0008_planning_workspace"',
        ))
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    with pytest.raises(RuntimeError):
        gate.check_migration_and_contract()


def test_exported_contract_bytes_are_reproducible_and_new_tables_have_no_raw_text():
    from scripts.export_m7_dispatch_schemas import TARGET, export

    assert TARGET.read_bytes() == export()
    forbidden = {"prompt", "output_json", "model_text", "api_key", "provider_url", "charge_microusd"}
    assert not forbidden.intersection(INVOCATIONS.c.keys())
    assert not forbidden.intersection(EVENTS.c.keys())
    assert "safe_inference_budget_scopes" not in Base.metadata.tables


def test_real_task_planning_lease_and_existing_budget_can_be_bound(kit):
    # Exercise the existing M6/M7 authority path too, not only minimal SQL fixtures.
    from safe_agent.contracts.inference_dispatch import bind_planning_attempt
    from safe_agent.storage.inference_budget import InferenceBudgetLedger
    from tests.m2.conftest import NOW as TASK_NOW
    from tests.m6.test_persistent_planning import setup
    from tests.m6.test_synthetic_workflow import new_task
    from tests.m7.test_inference_budget import limits, make_request

    client, signer, employee, _, _, _, store = setup(kit)
    task_id = new_task(client, signer, employee)
    lease = store.begin(employee, task_id, expected_task_version=0,
                        expected_workspace_version=0, mode="text_only", now=TASK_NOW)
    assert lease is not None
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=0, synthetic_mock=False, now=TASK_NOW)
    bound = bind_planning_attempt(req, lease)
    first = seal_invocation_event(bound, state="RESERVED", occurred_at=TASK_NOW)
    values = {
        **bound.model_dump(exclude={"schema_version"}), "binding_hash": bound.binding_hash,
        "state": "RESERVED", "state_version": 0, "claim_version": 0,
        "event_sequence": 1, "latest_event_hash": first.event_hash, "updated_at": TASK_NOW,
    }
    with kit["repo"].engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(INVOCATIONS.insert().values(**values))
    assert kit["repo"].replay_task(task_id).proposal_hash is None
