"""PR4 verifies provider-originated simulation is atomically bound to the outbox."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import update

pytest.importorskip("tal.office_execution")

from safe_agent.canonical import sha256
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.integrations.google_calendar.provider_simulation import GoogleProviderSimulationInvalid
from safe_agent.storage.models import GoogleTestOutboxBindingRow
from safe_agent.storage.repository import StateConflict
from tests.m2.conftest import NOW
from tests.m5.conftest import CALENDAR
from tests.m5.test_google_test_outbox import stage
from tests.m5.test_google_test_outbox import staging as source_staging


@pytest.fixture(name="staging")
def _reuse_staging(request: pytest.FixtureRequest):
    assert source_staging is not None
    return request.getfixturevalue("source_staging")


def test_pr4_google_intent_contains_a_sanitized_provider_simulation(staging):
    action = stage(staging).action_hash
    repo = staging["kit"]["repo"]
    bound = repo.read_google_test_action_binding("task_m2", action)
    assert bound["provider_simulation_hash"].startswith("sha256:")
    assert bound["observation_revision"].startswith("sha256:")
    assert repo.replay_task("task_m2").phase is OfficePhase.EXECUTING
    with repo._sessions() as db:
        row = db.get(GoogleTestOutboxBindingRow, action)
        assert row is not None
        assert row.provider_simulation_json is not None
        assert row.provider_simulation_hash == bound["provider_simulation_hash"]
        raw = json.dumps(row.provider_simulation_json)
        assert "google_test_calendar" in raw
        assert "holds_slot" in raw and '"holds_slot": false' in raw
        assert CALENDAR not in raw
        assert "test_refresh_token" not in raw
    assert staging["sim"].insert_calls == 0


def test_simulation_hash_tamper_breaks_reader_and_ledger_replay(staging):
    action = stage(staging).action_hash
    repo = staging["kit"]["repo"]
    with repo._sessions.begin() as db:
        db.execute(update(GoogleTestOutboxBindingRow).where(
            GoogleTestOutboxBindingRow.action_hash == action,
        ).values(provider_simulation_hash=sha256({"altered": True})))
    with pytest.raises(StateConflict, match="GOOGLE_PROVIDER_SIMULATION_INTEGRITY_ERROR"):
        repo.read_google_test_action_binding("task_m2", action)
    with pytest.raises(ValueError, match="Google provider simulation replay mismatch"):
        repo.replay_task("task_m2")
    assert staging["sim"].insert_calls == 0


def test_legacy_binding_without_simulation_is_not_dispatchable(staging):
    action = stage(staging).action_hash
    repo = staging["kit"]["repo"]
    with repo._sessions.begin() as db:
        db.execute(update(GoogleTestOutboxBindingRow).where(
            GoogleTestOutboxBindingRow.action_hash == action,
        ).values(provider_simulation_hash=None, provider_simulation_json=None))
    with pytest.raises(StateConflict, match="GOOGLE_PROVIDER_SIMULATION_INTEGRITY_ERROR"):
        repo.read_google_test_action_binding("task_m2", action)
    with pytest.raises(ValueError, match="Google outbox authorization/binding replay mismatch"):
        repo.replay_task("task_m2")
    assert staging["sim"].insert_calls == 0


def test_stager_refuses_observation_with_forged_revision_before_sql_write(staging, monkeypatch):
    service = staging["service"]
    real_observe = service.observe

    def altered(**kwargs):
        observed = real_observe(**kwargs)
        return observed.model_copy(update={"revision": "sha256:" + "9" * 64})

    monkeypatch.setattr(service, "observe", altered)
    with pytest.raises(GoogleProviderSimulationInvalid):
        stage(staging)
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.READY_TO_COMMIT
    assert staging["sim"].insert_calls == 0


def test_simulation_matches_only_exact_observation_and_action(staging):
    action = stage(staging).action_hash
    repo = staging["kit"]["repo"]
    binding = repo.read_google_test_action_binding("task_m2", action)
    with repo._sessions() as db:
        row = db.get(GoogleTestOutboxBindingRow, action)
        assert row is not None and row.provider_simulation_json is not None
        assert row.provider_simulation_hash == binding["provider_simulation_hash"]
    assert staging["sim"].insert_calls == 0
