"""W3 forward migration, frozen legacy successes and portable schema guards."""
from pathlib import Path

from sqlalchemy import inspect, select, text

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import parse_proposal_json
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.inference_results import recover_validated_result
from safe_agent.storage.repository import LedgerRepository
from safe_agent.storage.result_models import InferenceValidatedResultRow as ResultRow
from tests.m7.test_inference_coordinator import configured, held, transport
from tests.m7.test_inference_dispatch_schema import migrate, sqlite_engine
from tests.m7.test_inference_results import completed


def assert_schema(engine):
    actual = inspect(engine)
    table = ResultRow.__table__
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
        ("organization_id", "task_id", "attempt_id"),
    }


def test_forward_migration_keeps_legacy_success_non_reusable(tmp_path: Path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    migrate(path, "0011_inference_admission")
    engine = sqlite_engine(path)
    repo = LedgerRepository(engine)
    args, _, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    proposal = parse_proposal_json(OfflineMockInferenceTransport().infer(request).output_json)
    # Reproduce the pre-W3 finish event, without fabricating any saved payload.
    with store._locked(*args) as locked:
        original = store._transition(locked, "RESPONSE_VALIDATED", proposal_hash=sha256(proposal))
    before = held(repo)
    migrate(path)
    assert_schema(engine)
    wire = transport()
    calls = []
    monkeypatch.setattr(wire, "send", lambda *a: calls.append(a))
    runtime = TrustedInferenceCoordinator(admission, wire)
    result = runtime.resume(*args[:2])
    assert result.status == "LEGACY_MISSING" and result.outcome is None
    assert result.invocation == original
    assert runtime.execute(*args).outcome is None
    assert calls == [] and held(repo) == before
    with repo._sessions() as db:
        assert db.scalars(select(ResultRow)).all() == []
    with engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013_coordinated_planning"
    repo.close()


def test_migrated_result_survives_repeat_upgrade_and_reopen(tmp_path: Path):
    path = tmp_path / "current.sqlite"
    migrate(path)
    engine = sqlite_engine(path)
    assert_schema(engine)
    repo = LedgerRepository(engine)
    args, ticks, admission, _, _, _, expected = completed(repo)
    first = recover_validated_result(admission, *args[:2])
    before = held(repo)
    repo.close()
    migrate(path)
    fresh_engine = sqlite_engine(path)
    restarted = LedgerRepository(fresh_engine)
    from safe_agent.storage.inference_admission import TrustedInferenceAdmission
    from tests.m7.admission_fixtures import policy

    service = TrustedInferenceAdmission(restarted, policy=policy(), clock=lambda: ticks[0])
    try:
        recovered = recover_validated_result(service, *args[:2])
        assert recovered == first and recovered.outcome == expected
        assert held(restarted) == before
    finally:
        restarted.close()
