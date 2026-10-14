"""PR2: no Google write is reachable from a staged test connection.

Requires pinned upstream TAL in its dedicated CI job; default synthetic CI
still works when TAL isn't installed.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

pytest.importorskip("tal.office_execution")

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.canonical import sha256
from safe_agent.contracts.control import DecisionStatus
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.integrations.google_calendar.client import GoogleCalendarHTTP
from safe_agent.integrations.google_calendar.errors import GoogleConnectionDenied
from safe_agent.integrations.google_calendar.oauth import (
    GoogleCredentialVault,
    GoogleOAuthService,
    OAuthClientConfig,
)
from safe_agent.integrations.google_calendar.outbox import GoogleTestOutboxStager
from safe_agent.integrations.google_calendar.service import GoogleTestCalendarService
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import AccessDenied
from safe_agent.storage.models import GoogleTestOutboxBindingRow, OutboxRow
from safe_agent.storage.repository import MissingTask, StateConflict
from tests.m2.conftest import NOW, arrange
from tests.m5.conftest import CALENDAR, SimulatedGoogle


@pytest.fixture
def staging(kit):
    coordinator, preview, _, _, proposal = arrange(kit)
    decision = coordinator.propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=preview.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.AUTHORIZED
    actor = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes,
                   "task:execute", "connection:read", "connection:manage"),
    })
    sim = SimulatedGoogle()
    http = httpx.Client(transport=httpx.MockTransport(sim), trust_env=False)
    vault = GoogleCredentialVault(
        kit["repo"].engine, fernet_key=Fernet.generate_key(),
        allowed_test_calendar_ids=(CALENDAR,),
    )
    oauth = GoogleOAuthService(vault, OAuthClientConfig(
        client_id="m5_test_client", client_secret="test_secret_m5",
        redirect_uri="http://127.0.0.1:8000/oauth/google/callback",
    ), http)
    service = GoogleTestCalendarService(oauth, GoogleCalendarHTTP(http), kit["repo"])
    connection = vault.save(
        principal=actor, refresh_token="test_refresh_token_for_outbox_only",
        calendar_id=CALENDAR, now=NOW,
    )
    stager = GoogleTestOutboxStager(
        kit["repo"], kit["broker"], coordinator, service,
    )
    try:
        yield {
            "kit": kit, "actor": actor, "sim": sim, "vault": vault,
            "connection": connection, "stager": stager, "service": service,
        }
    finally:
        http.close()


def stage(env, *, principal=None, connection=None, now=NOW, version=None):
    kit = env["kit"]
    if version is None:
        version = kit["repo"].read_task("task_m2").state_version
    return env["stager"].stage(
        principal=principal or env["actor"],
        task_id="task_m2",
        connection_ref=connection or env["connection"],
        expected_state_version=version, now=now,
    )


def test_trusted_stage_is_atomic_and_cannot_be_claimed_by_m4_worker(staging):
    env, kit = staging, staging["kit"]
    version = kit["repo"].read_task("task_m2").state_version
    receipt = stage(env)
    assert receipt.provider_write_enabled is False
    assert receipt.status == "QUEUED_NO_PROVIDER_WORKER"
    assert kit["repo"].read_task("task_m2").state_version == version + 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.EXECUTING
    assert kit["repo"].read_outbox("task_m2")["provider_kind"] == "google_test"
    authorization = kit["repo"].read_office_execution_authorization(
        "task_m2", receipt.action_hash)
    binding = kit["repo"].read_google_test_action_binding(
        "task_m2", receipt.action_hash)
    assert binding["authorization_hash"] == authorization.authorization_hash
    assert binding["observation_revision"].startswith("sha256:")
    assert binding["provider_kind"] == "google_test"
    assert binding["connection_ref"] == env["connection"]
    assert CALENDAR not in str(binding)
    assert "test_refresh_token" not in str(binding)
    assert kit["repo"].claim_action(
        worker_id="synthetic_worker", task_id="task_m2", now=NOW) is None
    adapter = FakeCalendarAdapter(kit["office"]._source)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    assert worker.process_one(task_id="task_m2", now=NOW) is None
    assert adapter.effect_count == 0
    assert env["sim"].insert_calls == 0


def test_duplicate_staging_does_not_requeue_or_write(staging):
    receipt = stage(staging)
    with pytest.raises(StateConflict, match="GOOGLE_LOCAL_PROPOSAL_NOT_READY"):
        stage(staging)
    assert staging["kit"]["repo"].read_outbox("task_m2")["attempts"] == 0
    assert staging["kit"]["repo"].read_google_test_action_binding(
        "task_m2", receipt.action_hash)
    assert staging["sim"].insert_calls == 0


def test_provider_busy_interval_blocks_all_sql_intents(staging):
    staging["sim"].busy = [{
        "start": "2026-10-15T17:00:00Z",
        "end": "2026-10-15T17:30:00Z",
    }]
    with pytest.raises(StateConflict, match="GOOGLE_OBSERVED_SLOT_UNAVAILABLE"):
        stage(staging)
    kit = staging["kit"]
    assert kit["repo"].read_task("task_m2").phase is OfficePhase.READY_TO_COMMIT
    with pytest.raises(MissingTask):
        kit["repo"].read_outbox("task_m2")
    assert staging["sim"].insert_calls == 0


def test_wrong_actor_tenant_and_connection_are_not_authority(staging):
    bad_actor = staging["actor"].model_copy(update={"actor_id": "different_actor"})
    with pytest.raises(AccessDenied):
        stage(staging, principal=bad_actor)
    bad_org = staging["actor"].model_copy(update={"organization_id": "other_org"})
    with pytest.raises(AccessDenied):
        stage(staging, principal=bad_org)
    with pytest.raises(GoogleConnectionDenied):
        stage(staging, connection="gtest_missing_connection")
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.READY_TO_COMMIT
    assert staging["sim"].insert_calls == 0


def test_revoked_oauth_connection_never_creates_intent(staging):
    staging["vault"].revoke_local(
        principal=staging["actor"], connection_id=staging["connection"], now=NOW,
    )
    with pytest.raises(GoogleConnectionDenied):
        stage(staging)
    with pytest.raises(MissingTask):
        staging["kit"]["repo"].read_outbox("task_m2")
    assert staging["sim"].insert_calls == 0


def test_missing_tal_rolls_back_outbox_ledger_and_binding(staging, monkeypatch):
    import safe_agent.integrations.google_calendar.outbox as module

    original_import = module.import_module

    def unavailable(name):
        if name == "tal.runtime":
            raise ImportError("intentionally unavailable")
        return original_import(name)

    monkeypatch.setattr(module, "import_module", unavailable)
    before = staging["kit"]["repo"].read_task("task_m2")
    with pytest.raises(StateConflict, match="TAL_GOOGLE_AUTHORIZATION_UNAVAILABLE"):
        stage(staging)
    assert staging["kit"]["repo"].replay_task("task_m2") == before
    with pytest.raises(MissingTask):
        staging["kit"]["repo"].read_outbox("task_m2")
    assert staging["sim"].insert_calls == 0


def test_stale_state_version_and_expired_preview_are_rejected(staging):
    stale_version = staging["kit"]["repo"].read_task("task_m2").state_version - 1
    with pytest.raises(AccessDenied):
        stage(staging, version=stale_version)
    with pytest.raises(StateConflict):
        stage(staging, now=NOW + timedelta(minutes=11))
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.READY_TO_COMMIT
    assert staging["sim"].insert_calls == 0


def test_tampered_test_binding_is_not_accepted(staging):
    receipt = stage(staging)
    repo = staging["kit"]["repo"]
    with repo._sessions.begin() as session:
        session.execute(update(GoogleTestOutboxBindingRow).where(
            GoogleTestOutboxBindingRow.action_hash == receipt.action_hash,
        ).values(observation_hash=sha256({"tamper": True})))
    with pytest.raises(StateConflict, match="GOOGLE_OUTBOX_BINDING_INTEGRITY_ERROR"):
        repo.read_google_test_action_binding("task_m2", receipt.action_hash)
    with pytest.raises(ValueError, match="Google outbox authorization/binding replay mismatch"):
        repo.replay_task("task_m2")
    assert staging["sim"].insert_calls == 0


def test_request_hash_tampering_is_rejected_by_trusted_reader(staging):
    receipt = stage(staging)
    repo = staging["kit"]["repo"]
    with repo._sessions.begin() as session:
        session.execute(update(GoogleTestOutboxBindingRow).where(
            GoogleTestOutboxBindingRow.action_hash == receipt.action_hash,
        ).values(provider_request_hash=sha256({"wrong": "request"})))
    with pytest.raises(StateConflict, match="GOOGLE_OUTBOX_REQUEST_HASH_TAMPERED"):
        repo.read_google_test_action_binding("task_m2", receipt.action_hash)
    assert staging["sim"].insert_calls == 0


def test_connection_revoked_after_staging_denies_future_binding_read(staging):
    receipt = stage(staging)
    staging["vault"].revoke_local(
        principal=staging["actor"], connection_id=staging["connection"], now=NOW,
    )
    with pytest.raises(StateConflict, match="GOOGLE_BOUND_CONNECTION_UNAVAILABLE"):
        staging["kit"]["repo"].read_google_test_action_binding(
            "task_m2", receipt.action_hash)
    # Revocation blocks a future attempt; historical evidence remains replayable.
    assert staging["kit"]["repo"].replay_task(
        "task_m2").phase is OfficePhase.EXECUTING
    assert staging["sim"].insert_calls == 0


def test_approval_binding_expired_before_staging(kit):
    _, version, _, _, proposal = arrange(kit, approval=True)
    decision = kit["coordinator"].propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=version.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.APPROVAL_REQUIRED
    current = kit["repo"].read_task("task_m2")
    kit["coordinator"].approve(
        kit["manager"], "task_m2",
        ApprovalInput(
            proposal_hash=decision.proposal_hash,
            expected_state_version=current.state_version, approved=True,
            expires_at=NOW + timedelta(seconds=1),
        ), now=NOW,
    )
    actor = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute", "connection:read"),
    })
    # Readiness denial occurs before any Google provider request.
    denied = kit["coordinator"].revalidate_offline_readiness(
        actor, "task_m2", now=NOW + timedelta(seconds=2),
    )
    assert denied.decision is DecisionStatus.DENIED
    with pytest.raises(MissingTask):
        kit["repo"].read_outbox("task_m2")



def test_corrupted_provider_tag_never_routes_google_action_to_synthetic_worker(staging):
    """Defense in depth: an accidental provider tag flip cannot bypass the binding."""
    receipt = stage(staging)
    kit = staging["kit"]
    repo = kit["repo"]
    # Simulate an inconsistent outbox row. Even if a tag is changed, the
    # independently stored immutable connection binding forbids fake dispatch.
    with repo._sessions.begin() as session:
        session.execute(update(OutboxRow).where(
            OutboxRow.action_hash == receipt.action_hash,
        ).values(provider_kind="synthetic"))
    assert repo.claim_action(
        worker_id="synthetic_worker", task_id="task_m2", now=NOW,
    ) is None
    adapter = FakeCalendarAdapter(kit["office"]._source)
    worker = SyntheticRecoveryWorker(repo, kit["broker"], adapter)
    assert worker.process_one(task_id="task_m2", now=NOW) is None
    assert repo.read_outbox("task_m2")["attempts"] == 0
    assert adapter.effect_count == 0
    assert staging["sim"].insert_calls == 0
