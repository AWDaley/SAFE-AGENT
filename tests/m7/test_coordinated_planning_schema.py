"""W4 forward migration preserves old inference evidence and accounting."""
from pathlib import Path

from sqlalchemy import inspect, select, text

from safe_agent.storage.coordinated_models import (
    CoordinatedPlanningConsumptionRow,
    CoordinatedPlanningInputRow,
)
from safe_agent.storage.inference_results import recover_validated_result
from safe_agent.storage.repository import LedgerRepository
from safe_agent.storage.result_models import InferenceValidatedResultRow
from tests.m7.test_inference_coordinator import held
from tests.m7.test_inference_dispatch_schema import migrate, sqlite_engine
from tests.m7.test_inference_results import completed


def assert_planning_schema(engine):
    actual = inspect(engine)
    for model in (CoordinatedPlanningInputRow, CoordinatedPlanningConsumptionRow):
        table = model.__table__
        assert {c["name"] for c in actual.get_columns(table.name)} == set(table.columns.keys())
        assert {c["name"] for c in actual.get_check_constraints(table.name)} == {
            c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        assert {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                for f in actual.get_foreign_keys(table.name)} == {
            (tuple(c.parent.name for c in f.elements), f.referred_table.name,
             tuple(c.column.name for c in f.elements)) for f in table.foreign_key_constraints
        }
        assert {tuple(c["column_names"]) for c in actual.get_unique_constraints(table.name)} == {
            ("task_id",) if model is CoordinatedPlanningInputRow else ("request_id",),
        }


def test_w4_upgrade_preserves_w3_result_bytes_holds_and_history(tmp_path: Path):
    path = tmp_path / "w3_to_w4.sqlite"
    migrate(path, "0012_inference_results")
    repo = LedgerRepository(sqlite_engine(path))
    try:
        args, _, admission, _, _, _, expected = completed(repo)
        before = recover_validated_result(admission, *args[:2])
        budget = held(repo)
        history = repo.read_events(args[1].task_id)
        with repo._sessions() as db:
            raw = db.scalars(select(InferenceValidatedResultRow.record_json)).one()
        assert not inspect(repo.engine).has_table(CoordinatedPlanningInputRow.__tablename__)
        migrate(path)
        migrate(path)
        assert_planning_schema(repo.engine)
        after = recover_validated_result(admission, *args[:2])
        assert after == before and after.outcome == expected
        assert held(repo) == budget and repo.read_events(args[1].task_id) == history
        with repo._sessions() as db:
            assert db.scalars(select(InferenceValidatedResultRow.record_json)).one() == raw
            assert db.scalars(select(CoordinatedPlanningInputRow)).all() == []
            assert db.scalars(select(CoordinatedPlanningConsumptionRow)).all() == []
        with repo.engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014_billing_verifier_identity"
    finally:
        repo.close()
