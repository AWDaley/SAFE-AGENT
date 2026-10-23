"""Authored artifact/ledger fixtures only; no measured benchmark pilot or model call."""

import hashlib
import json
import math
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3 import TAU_COMMIT, calibration
from safe_agent.benchmarks.tau3.calibration import CalibrationError, calibrate_pilot
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.inventory import content_hash, pinned_source_manifest
from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    wire_digest,
)
from safe_agent.contracts.episodes import (
    AuthorizationBinding,
    BudgetDelta,
    EpisodeSpec,
    EpisodeState,
    OutgoingCall,
    RunSpec,
    arguments_digest,
    canonical_arguments_json,
)
from safe_agent.conversation.context_evidence import CONTEXT_RETENTION_PROFILE
from safe_agent.orchestration.episode_runtime import EpisodeRuntime, RunLimits, RuntimeLimits
from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
)
from safe_agent.storage.episodes import EpisodeStore

PRIVATE = "AUTHORED_PRIVATE_CALIBRATION_CANARY"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = "Authored public domain policy."
REVISION = {"commit": "a" * 40, "dirty": False, "runtime_sources_sha256": "b" * 64}
DEPENDENCIES = {name: "authored-format-fixture" for name in calibration._DEPENDENCIES}


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False))


def bind_artifacts(path, manifest):
    manifest["saved_artifacts_sha256"] = {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        for name in ("results.json", "official_metrics.json")
    }
    write(path / "manifest.json", manifest)


def change_results(path, change):
    manifest = json.loads((path / "manifest.json").read_text())
    results = json.loads((path / "results.json").read_text())
    change(results)
    write(path / "results.json", results)
    bind_artifacts(path, manifest)


@pytest.fixture
def authored_pilot(tmp_path, monkeypatch, request):
    """Construct real immutable journals and explicitly authored provider-format records."""
    path = tmp_path / "pilot"
    variant = getattr(request, "param", "provider")
    path.mkdir()
    (path / "private").mkdir()
    config = BenchmarkConfig(
        domain="retail", run_id="authored_pilot", task_ids=["0", "1"], num_trials=1,
        output_dir=str(path), development_task_ids=["0", "1"],
        episode_budget={"max_attempt_cost_microusd": 10},
    )
    limits = RuntimeLimits.model_validate(config.episode_budget)
    run_limits = RunLimits.model_validate(config.run_budget)
    pinned = pinned_source_manifest()
    count = sum(name.startswith("src/tau2/") or name == "pyproject.toml"
                or name.startswith("data/tau2/domains/retail/") for name in pinned["files"])
    inventory = {
        "source_verified": True, "status": "ready", "commit": TAU_COMMIT, "domain": "retail",
        "domain_task_count": 114, "selected_task_ids": ["0", "1"], "selected_task_count": 2,
        "task_ids_sha256": content_hash(["0", "1"]), "task_content_sha256": "c" * 64,
        "source_verification": {
            "verified": True, "manifest_sha256": content_hash(pinned), "required_files": count,
            "verified_files": count, "missing_files": [], "modified_files": [], "unsafe_paths": [],
            "unexpected_task_files": [],
        },
    }
    official = {name: getattr(config, name) for name in (
        "domain", "llm_agent", "llm_args_agent", "llm_user", "llm_args_user", "num_trials",
        "seed", "max_steps", "max_errors", "max_concurrency", "max_retries", "timeout",
    )}
    official.update(agent="safe_agent_" + config.run_id, user="user_simulator",
                    auto_review=False, auto_resume=False, enforce_communication_protocol=True)
    manifest = {
        "kind": "official_tau3_text", "tau_commit": TAU_COMMIT, "run_id": config.run_id,
        "system": "safe_agent", "runtime_mode": "canonical_safe", "status": "complete",
        "config": config.model_dump(), "config_sha256": content_hash(config.model_dump()),
        "evaluation_type": "ALL", "automatic_review": False, "retries": 0,
        "judge_model": "gpt-4.1-2025-04-14", "judge_args": {"temperature": 0.0},
        "safe_revision": REVISION, "dependencies": DEPENDENCIES, "official_config": official,
        "inventory": inventory, "scheduled": [{"task_id": str(i), "trial": 0} for i in range(2)],
        "expected_cost_components": ["agent", "user", "agent_provider_billing"],
    }
    info = {name: getattr(config, name) for name in ("num_trials", "seed", "max_steps", "max_errors")}
    for role in ("agent", "user"):
        info[role + "_info"] = {"implementation": official[role],
                                "llm": getattr(config, "llm_" + role),
                                "llm_args": getattr(config, "llm_args_" + role)}
    info["user_info"]["global_simulation_guidelines"] = "Authored public simulator guidelines."
    info["environment_info"] = {"domain_name": "retail", "policy": POLICY}
    seed = random.Random(config.seed).randint(0, 1000000)
    simulations = [{
        "task_id": str(i), "trial": 0, "seed": seed, "termination_reason": "user_stop",
        "reward_info": {"reward": float(i)}, "duration": 60.0, "agent_cost": None, "user_cost": None,
        "messages": [{"role": "assistant", "content": PRIVATE,
                      "raw_data": {"safe_episode_id": f"ep_{i}"}}], "policy": POLICY,
    } for i in range(2)]
    write(path / "results.json", {"info": info, "simulations": simulations})
    write(path / "official_metrics.json", {"avg_reward": 0.5, "total_simulations": 2})
    bind_artifacts(path, manifest)
    engine = create_engine("sqlite+pysqlite:///" + str(path / "private/episodes.sqlite"))
    store = EpisodeStore(engine)
    store.create_schema()
    EpisodePrivateContextRow.__table__.create(engine)
    EpisodeModelAttemptRow.__table__.create(engine)
    private = EpisodeRuntimeStore(engine)
    run_policy = wire_digest({
        "profile": "tau3-v1.0.1-episode-v1", "context_retention_profile": CONTEXT_RETENTION_PROFILE,
        "domain": config.domain, "model": config.llm_agent, "model_args": config.llm_args_agent,
        "episode_limits": limits.model_dump(), "run_limits": run_limits.model_dump(),
    })
    store.create_run(RunSpec(run_id=config.run_id, organization_id="benchmark", policy_hash=run_policy,
                             limits=run_limits.episode_limits(), created_at=NOW, deadline=at(86400)))
    held_by_episode = []
    for i in range(2):
        held_total = 0
        episode = f"ep_{i}"
        policy_hash = EpisodeRuntime._policy_hash("retail", POLICY, [])
        store.create_episode(EpisodeSpec(episode_id=episode, run_id=config.run_id,
            actor_id="benchmark_customer", domain="retail", policy_hash=policy_hash,
            limits=limits.episode_limits(), created_at=NOW, deadline=at(1800)))
        private.create(episode, {"episode_id": episode, "domain": "retail", "policy": POLICY,
            "policy_hash": policy_hash, "tools": [], "history": [{"content": PRIVATE}],
            "diagnostics": [{"code": "DOMAIN_SUBJECT_MISMATCH", "proposal_hash": sha256("denial")}]
                           if i else []})
        for j in range(i + 2):
            store.record_user_turn(episode, f"turn_{i}_{j}", sha256(PRIVATE), at(j + 1))
        for j in range((i + 1) * 2):
            request = EpisodeInferenceRequest(request_id=f"request_{i}_{j}", episode_id=episode,
                domain="retail", model="different-model" if variant == "model" else config.llm_agent,
                policy_hash=policy_hash,
                observation_hash=store.inspect(episode).observation_hash,
                projected_context={"authored_private_value": PRIVATE},
                max_output_tokens=1024 if variant == "output" else 2048,
                temperature=1.0 if variant == "temperature" else 0.0,
                seed=seed + 1 if variant == "seed" else seed,
                issued_at=at(10 + j * 2), deadline=at(80 + j * 2))
            held_tokens = len(request.model_dump_json().encode()) + 8192 + request.max_output_tokens
            if variant == "token_hold":
                held_tokens = 100
            held_total += held_tokens
            held_cost = (11 if j == 0 else 9 if j == 1 else 10) if variant == "cost_hold" else 10
            store.reserve_inference(episode, request.request_id, request.digest,
                BudgetDelta(model_calls=1, tokens=held_tokens, cost_microusd=held_cost), request.issued_at)
            private.prepare_inference(request)
            private.claim_inference(request)
            if variant == "uncertain" and i == 1 and j == 3:
                private.uncertain(request.request_id)
                store.mark_inference_uncertain(episode, request.request_id,
                                               sha256("authored-timeout"), at(11 + j * 2))
                continue
            # Provider-format usage is authored test data, not a real provider response.
            reply = EpisodeInferenceReply(request_id=request.request_id, input_hash=request.digest,
                model=request.model, output_json=("invalid-authored-reply" if i and j == 0 else
                    json.dumps({"kind": "message", "purpose": "answer", "text": PRIVATE})),
                input_tokens=held_tokens if variant == "reply_total" else 10,
                output_tokens=2049 if variant == "reply_output" else 5,
                usage_source="scripted" if variant == "scripted" else "provider_reported")
            private.complete_inference(request, reply)
            store.finish_inference(episode, request.request_id, reply.digest, at(11 + j * 2))
        held_by_episode.append(held_total)
        if variant == "uncertain" and i == 1:
            continue
        for j in range(i + 1):
            view = store.inspect(episode)
            args = canonical_arguments_json({"authored_private_value": PRIVATE})
            call = OutgoingCall(call_id=f"call_{i}_{j}", operation="fixture_read", arguments_json=args,
                arguments_hash=arguments_digest(args), policy_hash=policy_hash,
                observation_hash=view.observation_hash)
            auth = AuthorizationBinding(episode_id=episode, action_hash=call.action_hash,
                policy_hash=policy_hash, observation_hash=view.observation_hash,
                state_version=view.state_version, domain_admission_hash=sha256("authored-receipt"))
            store.prepare_call(episode, call, auth, view.state_version, at(30 + j * 2))
            store.consume_result(episode, call.call_id, sha256("authored-result"), bool(i), at(31 + j * 2))
        store.finish(episode, EpisodeState.FINISHED if i else EpisodeState.FAILED, at(60))
    engine.dispose()

    def inspection(candidate, root=None):
        if candidate.stage == "development":
            return {**inventory, "scheduled_conversations": 2}
        return {**inventory, "scheduled_conversations": 114 * candidate.num_trials}

    monkeypatch.setattr(calibration, "inspect_benchmark", inspection)
    monkeypatch.setattr(calibration, "_repository_revision", lambda: dict(REVISION))
    monkeypatch.setattr(calibration, "_current_dependencies", lambda: dict(DEPENDENCIES))
    target = config.model_copy(update={
        "stage": "coverage", "task_ids": None, "run_id": "target_run", "output_dir": str(tmp_path / "target"),
        "episode_budget": {"max_turns": 2, "max_model_calls": 2, "max_tool_calls": 1,
                           "max_tokens": 200, "max_cost_microusd": 10, "max_attempt_cost_microusd": 7},
        "run_budget": {"max_turns": 10, "max_model_calls": 10, "max_tool_calls": 10,
                       "max_tokens": 500, "max_cost_microusd": 100},
    })
    return path, target, held_by_episode


def test_calibration_uses_full_held_episode_maxima_and_preserves_failed_outcomes(authored_pilot):
    path, target, held = authored_pilot
    before = {str(file): file.read_bytes() for file in path.rglob("*") if file.is_file()}
    report = calibrate_pilot(path, target)
    assert report == calibrate_pilot(path, target)
    assert report["status"] == "review_required" and report["reasons"] == []
    expected = {"max_turns": 5, "max_model_calls": 6, "max_tool_calls": 3,
                "max_tokens": math.ceil(max(held) * 1.5), "max_cost_microusd": 42}
    assert report["derived_requirements"]["episode_budget"] == expected
    assert report["derived_requirements"]["run_budget"] == {name: amount * 114 for name, amount in expected.items()}
    assert report["derived_requirements"]["unchanged_attempt_hold_microusd"] == 7
    assert report["suggested_budget_patch"]["episode_budget"] == expected
    assert report["pilot"]["failed_graded_conversations"] == 1
    assert report["per_episode_usage"]["model_calls"]["sum"] == 6
    assert report["per_episode_usage"]["cost_microusd"]["sum"] == 60
    assert report["per_episode_usage"]["tokens"]["sum"] == sum(held)
    assert report["runtime_diagnostics"]["model_attempts"]["schema_rejected_replies"] == 1
    assert report["runtime_diagnostics"]["denials"]["domain_proposals"] == 1
    assert not report["pilot"]["recorded_actual_billing_complete"]
    assert not report["invoice_estimate"] and not report["statistical_tail_guarantee"]
    assert report["model_calls"] == report["writes_performed"] == 0
    assert PRIVATE not in json.dumps(report)
    assert before == {str(file): file.read_bytes() for file in path.rglob("*") if file.is_file()}
    assert not (path.parent / "target").exists()


def test_suggestions_never_lower_existing_caps_or_change_hold(authored_pilot):
    path, target, _ = authored_pilot
    target = target.model_copy(update={"episode_budget": {"max_attempt_cost_microusd": 0}, "run_budget": {}})
    report = calibrate_pilot(path, target, headroom_factor=2)
    assert report["status"] == "review_required"
    assert report["suggested_budget_patch"] == {"episode_budget": {}, "run_budget": {}}
    assert report["derived_requirements"]["episode_budget"]["max_cost_microusd"] == 0
    assert report["derived_requirements"]["unchanged_attempt_hold_microusd"] == 0


def test_invalid_target_cap_containment_is_rejected_before_any_suggestion(authored_pilot):
    path, target, _ = authored_pilot
    target = target.model_copy(update={"episode_budget": {
        **target.episode_budget, "max_model_calls": 1000,
    }})
    with pytest.raises(CalibrationError, match="^CALIBRATION_TARGET_CAP_CONTAINMENT_INVALID$"):
        calibrate_pilot(path, target)


@pytest.mark.parametrize("mutation,reason", [
    (lambda value: value["simulations"].pop(), "PILOT_RESULT_MATRIX_INCOMPLETE"),
    (lambda value: value["simulations"][0].update(termination_reason="max_steps"), "PILOT_HAS_CENSORED_TRAJECTORIES"),
    (lambda value: value["simulations"][0].update(reward_info=None), "PILOT_HAS_UNGRADED_RESULTS"),
    (lambda value: value["simulations"][0].update(messages=[]), "PILOT_LEDGER_RESULT_JOIN_INCOMPLETE"),
])
def test_incomplete_or_censored_results_never_drop_bad_samples(authored_pilot, mutation, reason):
    path, target, _ = authored_pilot
    change_results(path, mutation)
    report = calibrate_pilot(path, target)
    assert report["status"] == "unavailable" and reason in report["reasons"]
    assert report["suggested_budget_patch"] is None
    assert report["per_episode_usage"]["model_calls"]["sum"] == 6


def test_real_evidence_is_required_and_missing_files_are_not_created(tmp_path):
    absent = tmp_path / PRIVATE
    report = calibrate_pilot(absent, BenchmarkConfig())
    assert report["reasons"] == ["PILOT_MANIFEST_MISSING"]
    assert report["suggested_budget_patch"] is None and not absent.exists()
    assert PRIVATE not in json.dumps(report)


def test_stale_sources_condition_or_runtime_withhold_suggestions(authored_pilot, monkeypatch):
    path, target, _ = authored_pilot
    monkeypatch.setattr(calibration, "_repository_revision", lambda: {**REVISION, "runtime_sources_sha256": "d" * 64})
    monkeypatch.setattr(calibration, "_current_dependencies", lambda: {})
    monkeypatch.setattr(calibration, "inspect_benchmark", lambda *args: {"status": "source_mismatch"})
    target = target.model_copy(update={"llm_agent": "another-model"})
    report = calibrate_pilot(path, target)
    assert {"PILOT_RUNTIME_CHANGED", "PILOT_DEPENDENCIES_CHANGED", "TARGET_PINNED_SOURCES_UNAVAILABLE",
            "PILOT_CONDITION_MISMATCH"} <= set(report["reasons"])
    assert report["suggested_budget_patch"] is None


@pytest.mark.parametrize("authored_pilot", ["scripted", "uncertain"], indirect=True)
def test_scripted_or_uncertain_attempts_never_support_recommendations(authored_pilot):
    path, target, _ = authored_pilot
    report = calibrate_pilot(path, target)
    assert report["status"] == "unavailable"
    assert "PILOT_COMPLETE_PROVIDER_USAGE_REQUIRED" in report["reasons"]
    assert report["suggested_budget_patch"] is None
    assert report["per_episode_usage"]["model_calls"]["sum"] == 6
    assert report["per_episode_usage"]["cost_microusd"]["sum"] == 60
    if report["runtime_diagnostics"]["model_attempts"]["journal_uncertain"]:
        assert "PILOT_HAS_UNRESOLVED_ATTEMPTS" in report["reasons"]


@pytest.mark.parametrize("authored_pilot", ["model", "temperature", "output", "seed"], indirect=True)
def test_hash_valid_requests_must_match_pilot_and_official_trial_settings(authored_pilot):
    path, target, _ = authored_pilot
    with pytest.raises(CalibrationError, match="^PILOT_REQUEST_SETTINGS_MISMATCH$"):
        calibrate_pilot(path, target)


@pytest.mark.parametrize("authored_pilot,code", [
    ("token_hold", "PILOT_RESERVATION_REQUEST_MISMATCH"),
    ("cost_hold", "PILOT_RESERVATION_REQUEST_MISMATCH"),
    ("reply_total", "PILOT_REPLY_USAGE_INVALID"),
    ("reply_output", "PILOT_REPLY_USAGE_INVALID"),
], indirect=["authored_pilot"])
def test_hash_valid_reservations_and_usage_must_match_canonical_runtime(authored_pilot, code):
    path, target, _ = authored_pilot
    with pytest.raises(CalibrationError, match="^" + code + "$"):
        calibrate_pilot(path, target)


@pytest.mark.parametrize("factor", [0.5, 11, float("nan"), float("inf"), True])
def test_invalid_multiplier_is_rejected_before_reading_inputs(tmp_path, factor):
    with pytest.raises(CalibrationError, match="^CALIBRATION_HEADROOM_FACTOR_INVALID$"):
        calibrate_pilot(tmp_path, BenchmarkConfig(), headroom_factor=factor)


def test_changed_saved_rewards_and_duplicate_pairs_are_rejected(authored_pilot):
    path, target, _ = authored_pilot
    results = json.loads((path / "results.json").read_text())
    results["simulations"][0]["reward_info"]["reward"] = 0.75
    write(path / "results.json", results)
    with pytest.raises(CalibrationError, match="^PILOT_EVIDENCE_INVALID$"):
        calibrate_pilot(path, target)
    change_results(path, lambda value: value["simulations"].append(value["simulations"][0]))
    with pytest.raises(CalibrationError, match="^PILOT_EVIDENCE_INVALID$"):
        calibrate_pilot(path, target)


def test_corrupt_ledger_is_rejected_without_private_error_text(authored_pilot):
    path, target, _ = authored_pilot
    with sqlite3.connect(path / "private/episodes.sqlite") as db:
        db.execute("UPDATE safe_episodes SET state_version=state_version+1, snapshot_json=? "
                   "WHERE episode_id='ep_0'", (PRIVATE,))
    with pytest.raises(CalibrationError) as error:
        calibrate_pilot(path, target)
    assert PRIVATE not in str(error.value)
    assert str(error.value) == "CALIBRATION_INPUT_INVALID"
