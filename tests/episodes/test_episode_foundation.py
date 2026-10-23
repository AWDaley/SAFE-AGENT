"""Episode protocol acceptance: exact outbox, restart, budgets and replay."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from safe_agent.canonical import sha256
from safe_agent.contracts.episodes import (
    AuthorizationBinding,
    BudgetDelta,
    EpisodeLimits,
    EpisodeSpec,
    EpisodeState,
    OutgoingCall,
    RunSpec,
    arguments_digest,
    canonical_arguments_json,
)
from safe_agent.evidence.episodes import EpisodeBudgetExceeded, EpisodeConflict, replay_episode
from safe_agent.storage.episodes import EpisodeStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = sha256("domain policy")
LIMITS = EpisodeLimits(
    max_turns=20, max_model_calls=20, max_tool_calls=20,
    max_tokens=10000, max_cost_microusd=5000,
)


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'episodes.sqlite'}")
    result = EpisodeStore(engine)
    result.create_schema()
    yield result
    engine.dispose()


def seed(store, *, run_limits=LIMITS, episode_limits=LIMITS, episode_id="episode_1"):
    run = RunSpec(
        run_id="run_1", organization_id="synthetic_org", policy_hash=sha256("run config"),
        limits=run_limits, created_at=NOW, deadline=NOW + timedelta(hours=1),
    )
    store.create_run(run)
    episode = EpisodeSpec(
        episode_id=episode_id, run_id=run.run_id, actor_id="synthetic_customer",
        domain="retail", policy_hash=POLICY, limits=episode_limits,
        created_at=NOW, deadline=NOW + timedelta(minutes=30),
    )
    return run, episode, store.create_episode(episode)


def outgoing(snapshot, call_id="call_1", arguments=None):
    args = canonical_arguments_json(arguments or {"order_id": "private_order_123"})
    call = OutgoingCall(
        call_id=call_id, operation="get_order_details", arguments_json=args,
        arguments_hash=arguments_digest(args), policy_hash=snapshot.policy_hash,
        observation_hash=snapshot.observation_hash,
    )
    auth = AuthorizationBinding(
        episode_id=snapshot.episode_id, action_hash=call.action_hash,
        policy_hash=call.policy_hash, observation_hash=call.observation_hash,
        state_version=snapshot.state_version,
    )
    return call, auth


def test_exact_call_commits_before_emission_and_restart_never_resends(store):
    _, _, snapshot = seed(store)
    call, auth = outgoing(snapshot)
    prepared = store.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW)
    assert prepared.emit_allowed is True
    reopened = EpisodeStore(store.engine)
    recovered = reopened.get_call(snapshot.episode_id, call.call_id)
    assert recovered.call == call and recovered.emit_allowed is False
    duplicate = reopened.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW)
    assert duplicate.emit_allowed is False
    assert reopened.get_run("run_1").usage.tool_calls == 1
    assert "private_order_123" not in str(reopened.events(snapshot.episode_id))
    with store.engine.connect() as db:
        raw = db.execute(text("SELECT record_json FROM safe_episode_outgoing_calls")).scalar_one()
    assert "private_order_123" in raw  # Private host outbox contains the exact dispatch bytes.
    assert reopened.replay(snapshot.episode_id) == reopened.inspect(snapshot.episode_id)


def test_repeated_exact_result_is_consumed_once_and_multiple_calls_are_supported(store):
    _, _, snapshot = seed(store)
    for index in range(3):
        call, auth = outgoing(snapshot, f"call_{index}")
        store.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW)
        result_hash = sha256({"call": index, "ok": True})
        snapshot = store.consume_result(snapshot.episode_id, call.call_id, result_hash, True, NOW)
        assert store.consume_result(snapshot.episode_id, call.call_id, result_hash, True, NOW) == snapshot
        with pytest.raises(EpisodeConflict, match="replay changed"):
            store.consume_result(snapshot.episode_id, call.call_id, sha256("different"), True, NOW)
        with pytest.raises(EpisodeConflict, match="replay changed"):
            store.consume_result(snapshot.episode_id, call.call_id, result_hash, False, NOW)
    assert snapshot.state is EpisodeState.READY and snapshot.usage.tool_calls == 3
    assert store.replay(snapshot.episode_id) == snapshot


def test_uncertain_call_holds_budget_cannot_resend_and_late_result_is_correlated(store):
    _, _, snapshot = seed(store)
    call, auth = outgoing(snapshot)
    store.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW)
    uncertain = store.mark_call_uncertain(snapshot.episode_id, call.call_id, sha256("timeout"), NOW)
    assert uncertain.state is EpisodeState.OUTCOME_UNCERTAIN
    assert store.get_call(snapshot.episode_id, call.call_id).uncertain is True
    assert store.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW).emit_allowed is False
    other_call, other_auth = outgoing(uncertain, "call_new")
    with pytest.raises(EpisodeConflict):
        store.prepare_call(snapshot.episode_id, other_call, other_auth, uncertain.state_version, NOW)
    with pytest.raises(EpisodeConflict):
        store.consume_result(snapshot.episode_id, "wrong_call", sha256("result"), True, NOW)
    assert store.get_run("run_1").usage.tool_calls == 1
    resolved = store.consume_result(snapshot.episode_id, call.call_id, sha256("result"), True, NOW)
    assert resolved.state is EpisodeState.READY
    assert store.get_call(snapshot.episode_id, call.call_id).uncertain is False


def test_inference_result_binding_is_idempotent_and_unknown_holds_survive_new_trial(store):
    run_limits = LIMITS.model_copy(update={"max_model_calls": 2, "max_tokens": 100})
    ep_limits = run_limits
    run, spec, snapshot = seed(store, run_limits=run_limits, episode_limits=ep_limits)
    delta = BudgetDelta(model_calls=1, tokens=60, cost_microusd=30)
    reservation = store.reserve_inference(spec.episode_id, "request_1", sha256("input"), delta, NOW)
    assert reservation == store.reserve_inference(
        spec.episode_id, "request_1", sha256("input"), delta, NOW
    )
    assert store.get_reservation(spec.episode_id, "request_1") == reservation
    with pytest.raises(EpisodeConflict):
        store.reserve_inference(spec.episode_id, "request_1", sha256("other input"), delta, NOW)
    ready = store.finish_inference(spec.episode_id, "request_1", sha256("answer"), NOW)
    assert ready == store.finish_inference(spec.episode_id, "request_1", sha256("answer"), NOW)
    assert store.get_reservation(spec.episode_id, "request_1").result_hash == sha256("answer")
    with pytest.raises(EpisodeConflict):
        store.finish_inference(spec.episode_id, "request_1", sha256("other answer"), NOW)
    next_delta = BudgetDelta(model_calls=1, tokens=40, cost_microusd=20)
    store.reserve_inference(spec.episode_id, "request_2", sha256("next input"), next_delta, NOW)
    store.mark_inference_uncertain(spec.episode_id, "request_2", sha256("network unknown"), NOW)
    assert store.get_reservation(spec.episode_id, "request_2").uncertain
    store.finish(spec.episode_id, EpisodeState.FAILED, NOW)
    second = spec.model_copy(update={"episode_id": "episode_2"})
    store.create_episode(second)
    assert store.create_run(run).usage == BudgetDelta(model_calls=2, tokens=100, cost_microusd=50)
    assert store.create_episode(spec).usage == BudgetDelta(model_calls=2, tokens=100, cost_microusd=50)
    with pytest.raises(EpisodeBudgetExceeded):
        store.reserve_inference(second.episode_id, "request_3", sha256("input"), next_delta, NOW)
    assert store.inspect(second.episode_id).state_version == 0
    assert store.get_run("run_1").usage.tokens == 100
    assert store.replay(spec.episode_id).pending_request_id == "request_2"


def test_concurrent_episodes_atomically_share_last_run_allowance(store):
    limit = LIMITS.model_copy(update={"max_tool_calls": 1})
    _, spec, first = seed(store, run_limits=limit, episode_limits=limit)
    second = store.create_episode(spec.model_copy(update={"episode_id": "episode_2"}))
    barrier = Barrier(2)

    def worker(snapshot):
        call, auth = outgoing(snapshot)
        barrier.wait(timeout=5)
        try:
            return store.prepare_call(snapshot.episode_id, call, auth, snapshot.state_version, NOW)
        except EpisodeBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(worker, (first, second)))
    assert sum(receipt is not None for receipt in receipts) == 1
    assert store.get_run("run_1").usage.tool_calls == 1
    assert sum(store.inspect(ep).usage.tool_calls for ep in (first.episode_id, second.episode_id)) == 1


def test_authorization_binds_exact_arguments_policy_epoch_scope_and_consent(store):
    _, _, snapshot = seed(store)
    call, auth = outgoing(snapshot)
    for changed in (
        {"episode_id": "episode_else"}, {"state_version": 999},
        {"policy_hash": sha256("wrong policy")}, {"observation_hash": sha256("old")},
        {"action_hash": sha256("other action")}, {"consent_hash": sha256("unbound consent")},
    ):
        with pytest.raises(EpisodeConflict):
            store.prepare_call(snapshot.episode_id, call, auth.model_copy(update=changed), 0, NOW)
    changed = store.record_observation(snapshot.episode_id, sha256("new trusted observation"), 0, NOW)
    with pytest.raises(EpisodeConflict, match="stale"):
        store.prepare_call(snapshot.episode_id, call, auth, 0, NOW)
    with pytest.raises(EpisodeConflict):
        store.prepare_call(snapshot.episode_id, call, auth, changed.state_version, NOW)
    assert store.get_run("run_1").usage.tool_calls == 0
    assert store.inspect(snapshot.episode_id) == changed


def test_unicode_native_bytes_and_finite_float_arguments_remain_exact(store):
    _, _, snapshot = seed(store)
    decomposed = {"label": "e\u0301", "amount": 12.5}
    composed = {"label": "é", "amount": 12.5}
    first = canonical_arguments_json(decomposed)
    second = canonical_arguments_json(composed)
    assert arguments_digest(first) != arguments_digest(second)
    call, auth = outgoing(snapshot, arguments=decomposed)
    other_call, _ = outgoing(snapshot, arguments=composed)
    assert call.effect_hash != other_call.effect_hash and call.action_hash != other_call.action_hash
    store.prepare_call(snapshot.episode_id, call, auth, 0, NOW)
    recovered = EpisodeStore(store.engine).get_call(snapshot.episode_id, call.call_id)
    assert recovered.call.arguments_json.encode() == first.encode()
    with pytest.raises(ValueError):
        canonical_arguments_json({"amount": float("nan")})
    with pytest.raises(ValueError):
        arguments_digest('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        arguments_digest('{ "a": 1 }')
    with pytest.raises(ValidationError):
        OutgoingCall.model_validate({**call.model_dump(), "arguments_hash": sha256("forged")})


def test_turn_response_transition_dedup_and_terminal_replay(store):
    _, spec, snapshot = seed(store)
    turn = store.record_user_turn(spec.episode_id, "user_1", sha256("hello"), NOW)
    assert store.record_user_turn(spec.episode_id, "user_1", sha256("hello"), NOW) == turn
    awaiting = store.record_response(spec.episode_id, "response_1", sha256("confirm?"), True, NOW)
    assert awaiting.state is EpisodeState.AWAITING_CUSTOMER
    with pytest.raises(EpisodeConflict):
        store.reserve_inference(
            spec.episode_id, "request_1", sha256("input"), BudgetDelta(model_calls=1), NOW
        )
    ready = store.record_user_turn(spec.episode_id, "user_2", sha256("yes"), NOW)
    assert ready.state is EpisodeState.READY and ready.usage.turns == 2
    with pytest.raises(EpisodeConflict, match="changed"):
        store.record_user_turn(spec.episode_id, "user_2", sha256("no"), NOW)
    finished = store.finish(spec.episode_id, EpisodeState.FINISHED, NOW)
    assert store.finish(spec.episode_id, EpisodeState.FINISHED, NOW) == finished
    assert store.replay(spec.episode_id) == finished
    with pytest.raises(EpisodeConflict):
        store.record_user_turn(spec.episode_id, "user_3", sha256("more"), NOW)
    assert snapshot.observation_hash != ready.observation_hash


def test_deadline_stops_new_emissions_but_records_completion(store):
    _, spec, snapshot = seed(store)
    call, auth = outgoing(snapshot)
    store.prepare_call(spec.episode_id, call, auth, snapshot.state_version, NOW)
    late = spec.deadline + timedelta(seconds=1)
    ready = store.consume_result(spec.episode_id, call.call_id, sha256("completed"), True, late)
    other, authority = outgoing(ready, "call_2")
    with pytest.raises(EpisodeConflict, match="time bounds"):
        store.prepare_call(spec.episode_id, other, authority, ready.state_version, late)
    assert store.finish(spec.episode_id, EpisodeState.FINISHED, late).state is EpisodeState.FINISHED


def test_immutable_evidence_and_monotonic_aggregate_budget_are_database_guarded(store):
    _, spec, snapshot = seed(store)
    call, auth = outgoing(snapshot)
    store.prepare_call(spec.episode_id, call, auth, 0, NOW)
    statements = (
        "UPDATE safe_episode_outgoing_calls SET action_hash = 'changed'",
        "DELETE FROM safe_episode_events",
        "DELETE FROM safe_episodes",
        "DELETE FROM safe_episode_runs",
        "UPDATE safe_episode_runs SET tool_calls = 0, state_version = state_version + 1",
        "UPDATE safe_episodes SET state_version = state_version + 1, run_id = 'other'",
    )
    for statement in statements:
        with pytest.raises(IntegrityError), store.engine.begin() as db:
            db.execute(text(statement))
    assert store.get_run("run_1").usage.tool_calls == 1
    assert store.replay(spec.episode_id).state is EpisodeState.TOOL_PENDING


def test_replay_rejects_modified_reordered_or_missing_evidence(store):
    _, spec, _ = seed(store)
    store.record_user_turn(spec.episode_id, "message_1", sha256("request"), NOW)
    events = store.events(spec.episode_id)
    with pytest.raises(EpisodeConflict):
        replay_episode(events[1:])
    with pytest.raises(EpisodeConflict):
        replay_episode(reversed(events))
    altered = events[1].model_copy(update={"payload": {"message_id": "forged"}})
    with pytest.raises(EpisodeConflict, match="digest"):
        replay_episode((events[0], altered))


def test_closed_domain_and_limits_and_no_negative_or_coerced_budget():
    with pytest.raises(ValidationError):
        EpisodeSpec(
            episode_id="ep_1", run_id="run_1", actor_id="customer_1", domain="calendar",
            policy_hash=POLICY, limits=LIMITS, created_at=NOW, deadline=NOW + timedelta(hours=1),
        )
    for delta in ({"tokens": -1}, {"model_calls": True}, {"tokens": "2"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            BudgetDelta.model_validate(delta)
