"""Authored offline scheduler probes, never benchmark measurements or model calls."""

import json
import random
import stat
from types import SimpleNamespace

import pytest

from safe_agent.benchmarks.tau3 import TAU_COMMIT
from safe_agent.benchmarks.tau3.adapter import SafeTauAgent
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.execution import (
    INDEX_PATH,
    EpisodeIndex,
    load_episode_index,
    run_controlled,
)
from safe_agent.benchmarks.tau3.inventory import content_hash
from safe_agent.benchmarks.tau3.protocol import Decision
from safe_agent.benchmarks.tau3.reporting import report_saved_run, summarize_saved_results
from safe_agent.benchmarks.tau3.runner import register_safe_agent, text_run_config

PRIVATE = "PRIVATE_CONTROLLED_RUN_MESSAGE_CANARY"


def manifest_for(config, tasks):
    return {
        "kind": "official_tau3_text",
        "tau_commit": TAU_COMMIT,
        "run_id": config.run_id,
        "system": config.system,
        "config": config.model_dump(),
        "config_sha256": content_hash(config.model_dump()),
        "scheduled": [
            {"task_id": task, "trial": trial}
            for task in tasks
            for trial in range(config.num_trials)
        ],
        "expected_cost_components": ["agent", "user"],
    }


class Runtime:
    def __init__(self, decisions=(), *, failure=None):
        self.decisions = iter(decisions)
        self.failure = failure
        self.closed = 0
        self.seeds = []

    def start_episode(self, **kwargs):
        return 0

    def step(self, observation, state):
        if self.failure:
            raise self.failure
        return next(self.decisions), state + 1

    def set_seed(self, seed):
        self.seeds.append(seed)

    def close_episode(self, state, *, reason):
        self.closed += 1


def offline_upstream(monkeypatch):
    pytest.importorskip("tau2")
    import litellm

    def no_model(*args, **kwargs):
        raise AssertionError("Controlled scheduler fixtures must never invoke a model")

    monkeypatch.setattr(litellm, "completion", no_model)
    monkeypatch.setattr(litellm, "acompletion", no_model)


@pytest.fixture
def authored_harness(monkeypatch):
    offline_upstream(monkeypatch)
    from tau2.data_model.message import UserMessage
    from tau2.data_model.tasks import Action, EvaluationCriteria, RewardType, Task, UserScenario
    from tau2.domains.retail.environment import get_environment
    from tau2.registry import registry
    from tau2.runner import helpers
    from tau2.user.user_simulator_base import HalfDuplexUser

    environment = get_environment()
    user_id, record = next(iter(environment.tools.db.users.items()))
    original_address = record.address.model_dump()
    address = {
        "user_id": user_id,
        "address1": "103 Authored Probe Avenue",
        "address2": "",
        "city": "Boston",
        "state": "MA",
        "country": "USA",
        "zip": "02110",
    }
    tasks = [
        Task(
            id=name,
            user_scenario=UserScenario(instructions="Authored offline scheduler probe"),
            evaluation_criteria=EvaluationCriteria(
                actions=[
                    Action(
                        action_id="authored_write", name="modify_user_address", arguments=address
                    )
                ],
                reward_basis=[RewardType.DB],
            ),
        )
        for name in ("authored_a", "authored_b")
    ]
    monkeypatch.setattr(helpers, "get_tasks", lambda *args, **kwargs: tasks)
    fresh_environments = []

    def checked_environment(**kwargs):
        env = get_environment(**kwargs)
        fresh_environments.append(
            env.tools.db.users[user_id].address.model_dump() == original_address
        )
        return env

    monkeypatch.setitem(registry._domains, "retail", checked_environment)

    class User(HalfDuplexUser):
        def __init__(self, **kwargs):
            pass

        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            return UserMessage(role="user", content=PRIVATE, cost=0.0), state + 1

        def set_seed(self, seed):
            pass

    monkeypatch.setitem(registry._users, "authored_controlled_user", User)
    yield SimpleNamespace(
        tasks=tasks, address=address, resets=fresh_environments, registry=registry
    )
    for name in list(registry._agent_factories):
        if name.startswith("safe_agent_authored_controlled_"):
            registry._agent_factories.pop(name, None)
            registry._agent_metadata.pop(name, None)


def make_run(tmp_path, fixture, name, *, failure=None, repeats=2):
    output = tmp_path / name
    output.mkdir()
    config = BenchmarkConfig(
        domain="retail",
        run_id="authored_controlled_" + name,
        task_ids=[task.id for task in fixture.tasks],
        num_trials=repeats,
        output_dir=str(output),
        stop_after_infrastructure_errors=1,
    )
    manifest = manifest_for(config, config.task_ids)
    index = EpisodeIndex(output, manifest)
    runtimes = []

    def factory(context):
        runtime = Runtime(
            [
                Decision(
                    kind="tool",
                    tool_name="modify_user_address",
                    arguments=fixture.address,
                    call_id="authored_write",
                    authorization_id="authored_auth",
                ),
                Decision(kind="message", content="Authored completion", stop=True),
            ],
            failure=failure,
        )
        runtimes.append(runtime)
        return runtime

    agent = register_safe_agent(
        config, output_dir=output, runtime_factory=factory, lifecycle=index.lifecycle
    )
    official = text_run_config(config, agent_name=agent, output_dir=output)
    official.user = "authored_controlled_user"
    return output, config, official, manifest, index, runtimes


def test_controlled_success_matches_pinned_schedule_seeds_results_and_resets(
    authored_harness, tmp_path
):
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.runner.batch import run_tasks

    fixture = authored_harness
    output, config, official, manifest, index, runtimes = make_run(tmp_path, fixture, "serial")
    controlled = run_controlled(official, output_dir=output, index=index, error_limit=1)
    assert index.data["status"] == "complete"
    assert len(controlled.simulations) == len(runtimes) == 4
    assert all(runtime.closed == 1 for runtime in runtimes)
    assert all(fixture.resets)
    seeded = random.Random(config.seed)
    seeds = [seeded.randint(0, 1000000) for _ in range(config.num_trials)]
    assert [(row.task_id, row.trial, row.seed) for row in controlled.simulations] == [
        (task.id, trial, seed) for trial, seed in enumerate(seeds) for task in fixture.tasks
    ]
    assert [runtime.seeds for runtime in runtimes] == [
        [seed] for seed in seeds for _ in fixture.tasks
    ]
    assert all(row.reward_info.reward == 1 for row in controlled.simulations)

    canonical_output, _, canonical, _, unused_index, canonical_runtimes = make_run(
        tmp_path, fixture, "canonical"
    )
    # The historical path has no SAFE scheduling callback; invoking this fixture
    # through the real upstream batch runner also verifies environment resets.
    factory = fixture.registry.get_agent_factory(canonical.agent)

    def no_index_factory(*args, **kwargs):
        agent = factory(*args, **kwargs)
        agent._lifecycle = None
        return agent

    fixture.registry._agent_factories[canonical.agent] = no_index_factory
    pinned = run_tasks(
        canonical,
        fixture.tasks,
        save_path=canonical_output / "results.json",
        save_dir=canonical_output,
        console_display=False,
    )
    assert [
        (r.task_id, r.trial, r.seed, r.reward_info.reward, r.termination_reason)
        for r in pinned.simulations
    ] == [
        (r.task_id, r.trial, r.seed, r.reward_info.reward, r.termination_reason)
        for r in controlled.simulations
    ]
    assert all(runtime.closed == 1 for runtime in canonical_runtimes)
    assert all(fixture.resets)
    assert compute_metrics(controlled).model_dump() == compute_metrics(pinned).model_dump()
    manifest.update(index.manifest_fields())
    raw = json.loads((output / "results.json").read_text())["simulations"]
    assert load_episode_index(output, manifest, raw) is not None
    assert stat.S_IMODE((output / INDEX_PATH).stat().st_mode) == 0o600


def test_first_infrastructure_error_retains_join_and_stops_remaining_pairs(
    authored_harness, tmp_path
):
    from tau2.metrics.agent_metrics import compute_metrics

    output, _, official, manifest, index, runtimes = make_run(
        tmp_path,
        authored_harness,
        "failure",
        failure=RuntimeError(PRIVATE),
    )
    result = run_controlled(official, output_dir=output, index=index, error_limit=1)
    assert len(runtimes) == len(result.simulations) == 1
    assert runtimes[0].closed == 1
    assert result.simulations[0].termination_reason == "infrastructure_error"
    assert result.simulations[0].reward_info is None
    assert result.simulations[0].messages == []
    manifest.update(index.manifest_fields())
    (output / "manifest.json").write_text(json.dumps(manifest))
    raw_metrics = compute_metrics(result).model_dump()
    (output / "official_metrics.json").write_text(json.dumps(raw_metrics))
    report = report_saved_run(output)
    assert report["run_status"] == "stopped"
    assert report["coverage"]["recorded_attempts"] == 1
    assert report["coverage"]["graded"] == 0
    assert len(report["coverage"]["not_run_pairs"]) == 3
    assert report["coverage"]["started_pairs"] == 1
    assert report["official_score"]["avg_reward"] is None
    assert report["official_score"]["pass_hat_ks"] is None
    assert report["official_metrics"] == raw_metrics
    assert report["conservative_operational_metrics"]["pass_hat_ks"]["1"] == 0
    assert report["pairs"][0]["official_duration_seconds"] is None
    assert report["pairs"][0]["duration_basis"] == "not_retained_in_infrastructure_placeholder"
    bound = index.data["rows"][0]
    assert bound["episode_id"] and bound["runtime_initialized"]
    assert bound["counters"]["runtime_steps_started"] == 1
    assert bound["simulation_id"] == result.simulations[0].id
    for value in (PRIVATE, bound["episode_id"], bound["simulation_id"]):
        assert value not in json.dumps(report)
        assert value not in (output / "progress.json").read_text()


@pytest.mark.parametrize(
    "interruption,reason",
    [(KeyboardInterrupt(), "keyboard_interrupt"), (SystemExit(3), "system_exit")],
)
def test_controlled_interrupt_finalizes_without_synthetic_reward_or_forced_exit(
    authored_harness, tmp_path, interruption, reason
):
    output, _, official, manifest, index, runtimes = make_run(
        tmp_path,
        authored_harness,
        "interrupt",
        failure=interruption,
    )
    result = run_controlled(official, output_dir=output, index=index, error_limit=1)
    assert result.simulations == []
    assert json.loads((output / "results.json").read_text())["simulations"] == []
    assert index.data["status"] == "interrupted" and index.data["stop_reason"] == reason
    assert runtimes[0].closed == 1
    manifest.update(index.manifest_fields())
    report = summarize_saved_results(manifest, [], episode_index=index.data)
    assert report["coverage"]["started_without_recorded_outcome_pairs"] == [("authored_a", 0)]
    assert len(report["coverage"]["not_run_pairs"]) == 3
    assert report["official_score"]["avg_reward"] is None


def test_lifecycle_binding_precedes_failed_start_and_messages_export_counts_only(tmp_path):
    config = BenchmarkConfig(task_ids=["authored"], output_dir=str(tmp_path))
    index = EpisodeIndex(tmp_path, manifest_for(config, config.task_ids))
    index.begin("authored", 0, 42)

    class BadStart(Runtime):
        def start_episode(self, **kwargs):
            raise RuntimeError(PRIVATE)

    agent = SafeTauAgent(
        tools=[],
        domain_policy="public authored policy",
        domain="retail",
        runtime=BadStart(),
        lifecycle=index.lifecycle,
    )
    with pytest.raises(RuntimeError, match=PRIVATE):
        agent.get_init_state()
    assert index.data["rows"][0]["episode_id"] is not None
    assert not index.data["rows"][0]["runtime_initialized"]
    assert PRIVATE not in (tmp_path / "progress.json").read_text()


def test_episode_index_rejects_reassigned_or_tampered_results(authored_harness, tmp_path):
    output, _, official, manifest, index, _ = make_run(
        tmp_path, authored_harness, "tamper", repeats=1
    )
    run_controlled(official, output_dir=output, index=index, error_limit=1)
    manifest.update(index.manifest_fields())
    raw = json.loads((output / "results.json").read_text())["simulations"]
    raw[0]["task_id"], raw[1]["task_id"] = raw[1]["task_id"], raw[0]["task_id"]
    with pytest.raises(ValueError, match="EPISODE_INDEX_BINDING_INVALID"):
        load_episode_index(output, manifest, raw)


def test_interrupt_after_upstream_checkpoint_recovers_recorded_outcome(
    authored_harness, tmp_path, monkeypatch
):
    from tau2.runner import checkpoint

    output, _, official, manifest, index, _ = make_run(
        tmp_path, authored_harness, "checkpoint_interrupt"
    )
    original = checkpoint.create_checkpoint_fns

    def checkpoint_then_interrupt(*args, **kwargs):
        save, replace = original(*args, **kwargs)

        def save_then_interrupt(simulation):
            save(simulation)
            raise KeyboardInterrupt

        return save_then_interrupt, replace

    monkeypatch.setattr(checkpoint, "create_checkpoint_fns", checkpoint_then_interrupt)
    result = run_controlled(official, output_dir=output, index=index, error_limit=1)
    assert len(result.simulations) == 1
    assert result.simulations[0].reward_info.reward == 1
    assert index.data["status"] == "interrupted"
    assert index.data["rows"][0]["state"] == "recorded"
    assert index.data["rows"][0]["graded"]
    manifest.update(index.manifest_fields())
    raw = json.loads((output / "results.json").read_text())["simulations"]
    assert load_episode_index(output, manifest, raw) is not None


def test_legacy_missing_outcomes_are_not_claimed_to_be_not_run():
    config = BenchmarkConfig(task_ids=["authored"])
    report = summarize_saved_results(manifest_for(config, config.task_ids), [])
    assert report["coverage"]["missing_pairs"] == [("authored", 0)]
    assert report["coverage"]["not_run_pairs"] is None
    assert report["coverage"]["started_pairs"] is None
    assert report["pairs"][0]["state"] == "unrecorded_status_unknown"
