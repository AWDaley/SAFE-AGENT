"""Mandatory in tau3 PostgreSQL CI; an actual migrated server is required.

Default local/office runs skip this module. Setting the explicit disposable-DB
flag makes a missing server, SQLite URL, absent migration, or skipped assertion
an error. No create_all or SQLite substitute is used for these acceptance tests.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from safe_agent.canonical import sha256
from safe_agent.contracts.episodes import BudgetDelta, EpisodeSpec, EpisodeState, RunSpec
from safe_agent.evidence.episodes import EpisodeBudgetExceeded, EpisodeConflict
from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
    RuntimeStorageDenied,
)
from safe_agent.storage.episodes import EPISODE_TABLES, EpisodeCallResultRow, EpisodeStore
from tests.episodes.test_episode_foundation import LIMITS, NOW, POLICY, outgoing
from tests.episodes.test_episode_model_boundary import reply, request

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_EPISODES") != "1",
    reason="requires the explicit disposable PostgreSQL episode acceptance job",
)
MODEL = EpisodeModelAttemptRow.__table__
CONTEXT = EpisodePrivateContextRow.__table__


@pytest.fixture(scope="module")
def pg():
    url = os.environ.get("SAFE_AGENT_DATABASE_URL", "")
    assert url.startswith("postgresql+psycopg://"), "a real isolated PostgreSQL server is mandatory"
    engine = create_engine(url, connect_args={"options": "-c statement_timeout=15000"})
    assert engine.dialect.name == "postgresql"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all() == ["0016_episode_inference"], (
            "apply the canonical migrations before PostgreSQL acceptance"
        )
        assert "PostgreSQL" in connection.execute(text("SELECT version()")).scalar_one()
    try:
        yield engine
    finally:
        engine.dispose()


def seed_postgres(engine, *, run_limits=LIMITS, episode_limits=LIMITS):
    key = uuid4().hex
    store = EpisodeStore(engine)
    run = RunSpec(
        run_id="run_" + key,
        organization_id="synthetic_pg_org",
        policy_hash=sha256("pg config"),
        limits=run_limits,
        created_at=NOW,
        deadline=NOW + timedelta(hours=1),
    )
    store.create_run(run)
    episode = EpisodeSpec(
        episode_id="episode_" + key,
        run_id=run.run_id,
        actor_id="synthetic_customer",
        domain="retail",
        policy_hash=POLICY,
        limits=episode_limits,
        created_at=NOW,
        deadline=NOW + timedelta(minutes=30),
    )
    return store, episode, store.create_episode(episode)


def denied_sql(engine, statement, parameters=None):
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text(statement), parameters or {})


def test_postgres_episode_migrations_have_real_constraints_and_trigger_functions(pg):
    expected = {
        "safe_episode_runs_mutation_guard",
        "safe_episodes_mutation_guard",
        "safe_episode_events_immutable",
        "safe_episode_inference_reservations_immutable",
        "safe_episode_inference_results_immutable",
        "safe_episode_outgoing_calls_immutable",
        "safe_episode_tool_results_immutable",
        "safe_episode_model_attempt_fence",
        "safe_episode_private_context_fence",
    }
    with pg.connect() as connection:
        triggers = set(
            connection.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgenabled != 'D'")
            ).scalars()
        )
        assert expected <= triggers
        inspector = inspect(connection)
        for table in (*EPISODE_TABLES, MODEL, CONTEXT):
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(
                table.columns.keys()
            )
        assert {
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys("safe_episode_tool_results")
        } == {
            (("episode_id", "call_id"), "safe_episode_outgoing_calls", ("episode_id", "call_id")),
        }


def test_postgres_all_episode_evidence_is_immutable_and_foreign_results_fail(pg):
    store, spec, view = seed_postgres(pg)
    request_id = "request_" + uuid4().hex
    store.reserve_inference(
        spec.episode_id,
        request_id,
        sha256("input"),
        BudgetDelta(model_calls=1, tokens=100, cost_microusd=5),
        NOW,
    )
    view = store.finish_inference(spec.episode_id, request_id, sha256("reply"), NOW)
    call, authority = outgoing(view, "call_" + uuid4().hex)
    store.prepare_call(spec.episode_id, call, authority, view.state_version, NOW)
    final = store.consume_result(spec.episode_id, call.call_id, sha256("result"), True, NOW)
    for table in EPISODE_TABLES[2:]:
        with pg.connect() as connection:
            assert connection.execute(
                select(table).where(table.c.episode_id == spec.episode_id)
            ).first()
        denied_sql(pg, f"DELETE FROM {table.name} WHERE episode_id=:id", {"id": spec.episode_id})
        denied_sql(
            pg,
            f"UPDATE {table.name} SET episode_id=episode_id WHERE episode_id=:id",
            {"id": spec.episode_id},
        )
    with pytest.raises(IntegrityError), pg.begin() as connection:
        connection.execute(
            EpisodeCallResultRow.__table__.insert().values(
                episode_id=spec.episode_id,
                call_id="nonexistent_call",
                result_hash=sha256("foreign"),
                success=True,
            )
        )
    assert store.replay(spec.episode_id) == final


def test_postgres_run_budget_and_episode_identity_cannot_rewind(pg):
    store, spec, view = seed_postgres(pg)
    call, authority = outgoing(view, "call_" + uuid4().hex)
    store.prepare_call(spec.episode_id, call, authority, view.state_version, NOW)
    for statement in (
        "UPDATE safe_episode_runs SET tool_calls=0, state_version=state_version+1 WHERE run_id=:id",
        "UPDATE safe_episode_runs SET spec_json='{}', state_version=state_version+1 WHERE run_id=:id",
        "UPDATE safe_episode_runs SET state_version=state_version+2 WHERE run_id=:id",
        "DELETE FROM safe_episode_runs WHERE run_id=:id",
    ):
        denied_sql(pg, statement, {"id": spec.run_id})
    for statement in (
        "UPDATE safe_episodes SET spec_hash=:changed, state_version=state_version+1 WHERE episode_id=:id",
        "UPDATE safe_episodes SET state_version=state_version-1 WHERE episode_id=:id",
        "DELETE FROM safe_episodes WHERE episode_id=:id",
    ):
        denied_sql(pg, statement, {"id": spec.episode_id, "changed": sha256("changed")})
    assert store.get_run(spec.run_id).usage.tool_calls == 1
    assert store.replay(spec.episode_id) == store.inspect(spec.episode_id)


def test_postgres_concurrent_same_call_has_one_emitter_and_no_restart_resend(pg):
    store, spec, view = seed_postgres(pg)
    call, authority = outgoing(view, "call_" + uuid4().hex)
    barrier = Barrier(2)

    def prepare(_):
        barrier.wait(timeout=10)
        return EpisodeStore(pg).prepare_call(
            spec.episode_id, call, authority, view.state_version, NOW
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(prepare, range(2)))
    assert sorted(receipt.emit_allowed for receipt in receipts) == [False, True]
    assert store.get_run(spec.run_id).usage.tool_calls == 1
    reopened = EpisodeStore(pg)
    assert not reopened.get_call(spec.episode_id, call.call_id).emit_allowed
    assert not reopened.prepare_call(
        spec.episode_id, call, authority, view.state_version, NOW
    ).emit_allowed
    uncertain = reopened.mark_call_uncertain(spec.episode_id, call.call_id, sha256("timeout"), NOW)
    assert uncertain.state == EpisodeState.OUTCOME_UNCERTAIN
    changed, changed_authority = outgoing(uncertain, "new_call_" + uuid4().hex)
    with pytest.raises(EpisodeConflict):
        reopened.prepare_call(
            spec.episode_id, changed, changed_authority, uncertain.state_version, NOW
        )
    assert reopened.get_run(spec.run_id).usage.tool_calls == 1
    assert reopened.replay(spec.episode_id) == uncertain


def test_postgres_concurrent_episodes_share_one_remaining_run_allowance(pg):
    limits = LIMITS.model_copy(update={"max_tool_calls": 1})
    store, spec, first = seed_postgres(pg, run_limits=limits, episode_limits=limits)
    second = store.create_episode(spec.model_copy(update={"episode_id": "episode_" + uuid4().hex}))
    barrier = Barrier(2)

    def prepare(view):
        call, authority = outgoing(view, "call_" + uuid4().hex)
        barrier.wait(timeout=10)
        try:
            return EpisodeStore(pg).prepare_call(
                view.episode_id, call, authority, view.state_version, NOW
            )
        except EpisodeBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(prepare, (first, second)))
    assert sum(receipt is not None for receipt in receipts) == 1
    assert store.get_run(spec.run_id).usage.tool_calls == 1
    views = [store.inspect(view.episode_id) for view in (first, second)]
    assert sorted(view.usage.tool_calls for view in views) == [0, 1]
    assert next(view for view in views if view.usage.tool_calls == 0).state == EpisodeState.READY


def test_postgres_model_attempt_fence_binds_reply_and_freezes_completion(pg):
    _, _, view = seed_postgres(pg)
    journal = EpisodeRuntimeStore(pg)
    req = request(view, request_id="request_" + uuid4().hex)
    journal.prepare_inference(req)
    result = reply(req)
    with pytest.raises(DBAPIError), pg.begin() as connection:
        connection.execute(
            MODEL.update()
            .where(MODEL.c.request_id == req.request_id)
            .values(
                status="COMPLETE",
                reply_json=result.model_dump_json(),
                reply_hash=result.digest,
            )
        )
    journal.claim_inference(req)
    mismatched = reply(req, model="foreign_model")
    with pytest.raises(DBAPIError), pg.begin() as connection:
        connection.execute(
            MODEL.update()
            .where(MODEL.c.request_id == req.request_id)
            .values(
                status="COMPLETE",
                reply_json=mismatched.model_dump_json(),
                reply_hash=mismatched.digest,
            )
        )
    journal.complete_inference(req, result)
    assert EpisodeRuntimeStore(pg).inference(req.request_id) == (req, result, "COMPLETE")
    for statement in (
        "UPDATE safe_episode_model_attempts SET status='PREPARED', reply_json=NULL, reply_hash=NULL WHERE request_id=:id",
        "UPDATE safe_episode_model_attempts SET reply_hash=:changed WHERE request_id=:id",
        "UPDATE safe_episode_model_attempts SET request_json='{}' WHERE request_id=:id",
        "DELETE FROM safe_episode_model_attempts WHERE request_id=:id",
    ):
        denied_sql(pg, statement, {"id": req.request_id, "changed": sha256("changed")})


def test_postgres_model_claim_concurrency_and_uncertainty_keep_reserved_cost(pg):
    store, spec, view = seed_postgres(pg)
    journal = EpisodeRuntimeStore(pg)
    req = request(view, request_id="request_" + uuid4().hex)
    budget = BudgetDelta(model_calls=1, tokens=100, cost_microusd=75)
    store.reserve_inference(spec.episode_id, req.request_id, req.digest, budget, NOW)
    journal.prepare_inference(req)
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=10)
        try:
            EpisodeRuntimeStore(pg).claim_inference(req)
            return True
        except RuntimeStorageDenied:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]
    journal.uncertain(req.request_id)
    store.mark_inference_uncertain(
        spec.episode_id, req.request_id, sha256("unknown_provider_outcome"), NOW
    )
    assert EpisodeRuntimeStore(pg).inference(req.request_id)[2] == "UNCERTAIN"
    with pytest.raises(RuntimeStorageDenied):
        EpisodeRuntimeStore(pg).claim_inference(req)
    denied_sql(
        pg,
        "UPDATE safe_episode_model_attempts SET status='PREPARED' WHERE request_id=:id",
        {"id": req.request_id},
    )
    assert store.get_run(spec.run_id).usage == budget
    assert store.get_reservation(spec.episode_id, req.request_id).uncertain
    assert store.replay(spec.episode_id).state == EpisodeState.OUTCOME_UNCERTAIN


def test_postgres_private_context_claim_is_atomic_and_old_observations_cannot_replay(pg):
    _, spec, _ = seed_postgres(pg)
    journal = EpisodeRuntimeStore(pg)
    journal.create(spec.episode_id, {"seen_observations": [], "private": "synthetic_context"})
    barrier = Barrier(2)

    def claim(index):
        barrier.wait(timeout=10)
        try:
            return EpisodeRuntimeStore(pg).claim(spec.episode_id, f"observation_{index}", 0)
        except RuntimeStorageDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    denied_sql(
        pg,
        "UPDATE safe_episode_private_contexts SET context_json='{}', version=version+1 WHERE episode_id=:id",
        {"id": spec.episode_id},
    )
    journal.save(spec.episode_id, winner, {"seen_observations": []})
    restored = EpisodeRuntimeStore(pg).load(spec.episode_id)
    assert restored.data["seen_observations"] == [winner.active_observation]
    with pytest.raises(RuntimeStorageDenied, match="OBSERVATION_ALREADY_CONSUMED"):
        journal.claim(spec.episode_id, winner.active_observation, restored.version)
    with pytest.raises(RuntimeStorageDenied):
        journal.save(spec.episode_id, winner, {})
    denied_sql(
        pg,
        "DELETE FROM safe_episode_private_contexts WHERE episode_id=:id",
        {"id": spec.episode_id},
    )
