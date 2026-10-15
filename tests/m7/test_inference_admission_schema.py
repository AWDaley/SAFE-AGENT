"""Forward 0010 -> 0011 migration and metadata parity on real SQLite schema."""

from pathlib import Path

from sqlalchemy import inspect, select, text

from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.models import InferenceAdmissionAuthorityRow, InferenceAdmissionRow
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7.test_inference_dispatch_schema import migrate, sqlite_engine


def test_forward_upgrade_preserves_0010_and_admission_persists(tmp_path: Path):
    path = tmp_path / "upgrade.sqlite"
    migrate(path, "0010_inference_dispatch")
    engine = sqlite_engine(path)
    repo = LedgerRepository(engine)
    args = assigned(repo)
    before = repo.read_events(args[1].task_id)
    migrate(path)
    actual = inspect(engine)
    for table in (InferenceAdmissionRow.__table__, InferenceAdmissionAuthorityRow.__table__):
        assert {c["name"] for c in actual.get_columns(table.name)} == set(table.columns.keys())
        assert {c["name"] for c in actual.get_check_constraints(table.name)} == {
            c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        assert {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                for f in actual.get_foreign_keys(table.name)} == {
            (tuple(c.parent.name for c in f.elements), f.referred_table.name,
             tuple(c.column.name for c in f.elements)) for f in table.foreign_key_constraints
        }
    first = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    migrate(path)  # Upgrade head twice must not clear evidence or holds.
    second = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    assert first.binding == second.binding
    assert before == repo.read_events(args[1].task_id)
    with engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011_inference_admission"
        assert db.execute(select(InferenceAdmissionRow.request_id)).scalars().all() == [first.binding.request_id]
    repo.close()
