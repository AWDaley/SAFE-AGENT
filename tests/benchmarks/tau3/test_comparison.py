"""Authored report-format fixtures only; these are not benchmark measurements."""

import hashlib
import json
import random
from pathlib import Path

import pytest

from safe_agent.benchmarks.tau3 import EXPECTED_TASK_COUNTS, TAU_COMMIT
from safe_agent.benchmarks.tau3.comparison import (
    ComparisonError,
    compare_saved_runs,
    paired_task_interval,
)
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.inventory import content_hash, pinned_source_manifest

PRIVATE = "AUTHORED_PRIVATE_TRANSCRIPT_OR_REVIEWER_CANARY"


def _write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False))


def _fixture(parent: Path, system: str, *, overrides=None, full_final=False):
    path = parent / system
    path.mkdir()
    values = {
        "domain": "retail",
        "system": system,
        "run_id": "authored_" + system,
        "num_trials": 4,
        "task_ids": ["a", "b"],
        "output_dir": str(path),
    }
    if full_final:
        values.update(stage="final", task_ids=None)
    values.update(overrides or {})
    config = BenchmarkConfig.model_validate(values)
    ids = [str(index) for index in range(114)] if full_final else ["a", "b"]
    pinned = pinned_source_manifest()
    count = sum(
        name.startswith("src/tau2/")
        or name == "pyproject.toml"
        or name.startswith("data/tau2/domains/retail/")
        for name in pinned["files"]
    )
    official = {
        name: getattr(config, name)
        for name in (
            "domain",
            "llm_agent",
            "llm_args_agent",
            "llm_user",
            "llm_args_user",
            "num_trials",
            "seed",
            "max_steps",
            "max_errors",
            "max_concurrency",
            "max_retries",
            "timeout",
        )
    }
    official.update(
        agent="safe_agent_" + config.run_id if system == "safe_agent" else "llm_agent",
        user="user_simulator",
        auto_review=False,
        auto_resume=False,
        enforce_communication_protocol=True,
        save_to=str(path),
        retrieval_config=None,
        retrieval_config_kwargs=None,
    )
    manifest = {
        "kind": "official_tau3_text",
        "tau_commit": TAU_COMMIT,
        "run_id": config.run_id,
        "system": system,
        "runtime_mode": "canonical_safe" if system == "safe_agent" else "stock_baseline",
        "status": "complete",
        "config": config.model_dump(),
        "config_sha256": content_hash(config.model_dump()),
        "evaluation_type": "ALL",
        "automatic_review": False,
        "retries": 0,
        "judge_model": "authored-judge",
        "judge_args": {"temperature": 0.0},
        "safe_revision": {"commit": "a" * 40, "dirty": False, "runtime_sources_sha256": "b" * 64},
        "dependencies": {"tau2": "authored_fixture"},
        "official_config": official,
        "inventory": {
            "source_verified": True,
            "status": "ready",
            "commit": TAU_COMMIT,
            "domain": "retail",
            "domain_task_count": EXPECTED_TASK_COUNTS["retail"],
            "selected_task_ids": ids,
            "selected_task_count": len(ids),
            "task_ids_sha256": content_hash(ids),
            "task_content_sha256": "c" * 64,
            "tool_inventory": {"AuthoredTools:is_tool": ["fixture_tool"]},
            "source_verification": {
                "verified": True,
                "manifest_sha256": content_hash(pinned),
                "required_files": count,
                "verified_files": count,
                "missing_files": [],
                "modified_files": [],
                "unsafe_paths": [],
                "unexpected_task_files": [],
            },
        },
        "scheduled": [
            {"task_id": task, "trial": trial} for task in ids for trial in range(config.num_trials)
        ],
        "expected_cost_components": ["agent", "user"]
        + (["agent_provider_billing"] if system == "safe_agent" else []),
    }
    rng = random.Random(config.seed)
    seeds = [rng.randint(0, 1000000) for _ in range(config.num_trials)]
    rows = [
        {
            "id": f"fixture_{task}_{trial}",
            "task_id": task,
            "trial": trial,
            "seed": seeds[trial],
            "termination_reason": "user_stop",
            "reward_info": {"reward": float(system == "safe_agent" and task == "a")},
            "duration": 2.0 if system == "safe_agent" else 1.0,
            "agent_cost": 0.0 if system == "safe_agent" else 0.5,
            "user_cost": 0.25,
            "messages": [{"role": "user", "content": PRIVATE}],
            "policy": "authored public policy",
        }
        for task in ids
        for trial in range(config.num_trials)
    ]
    info = {
        name: getattr(config, name) for name in ("num_trials", "seed", "max_steps", "max_errors")
    }
    for role in ("agent", "user"):
        info[role + "_info"] = {
            "implementation": official[role],
            "llm": getattr(config, "llm_" + role),
            "llm_args": getattr(config, "llm_args_" + role),
        }
    info["user_info"]["global_simulation_guidelines"] = "authored public simulator guidelines"
    info["environment_info"] = {"domain_name": "retail", "policy": "authored public policy"}
    _write(path / "manifest.json", manifest)
    _write(
        path / "results.json",
        {"info": info, "simulations": rows, "tasks": [{"private_fixture": PRIVATE}]},
    )
    return path


def _invoice(path: Path, *, allocate=True):
    manifest = json.loads((path / "manifest.json").read_text())
    row = {
        "category": "agent_provider_billing",
        "run_id": manifest["run_id"],
        "component": "agent",
        "currency": "USD",
        "scope": "all_agent_attempts_including_failures_and_repairs",
        "replaces": "upstream_agent_aggregate",
        "amount_usd": str(len(manifest["scheduled"])),
        "event_id": "authored-reviewed-total",
        "provider": "authored-provider",
        "source_sha256": "d" * 64,
        "reviewer": PRIVATE,
    }
    if allocate:
        row["allocations"] = [{**pair, "amount_usd": "1"} for pair in manifest["scheduled"]]
    (path / "additional_costs.jsonl").write_text(json.dumps(row) + "\n")


def _change(path, file, mutation):
    value = json.loads((path / file).read_text())
    mutation(value)
    _write(path / file, value)


def test_matched_task_bootstrap_and_resource_join_are_deterministic_and_private(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _invoice(left)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    report = compare_saved_runs(left, right, bootstrap_samples=1000, seed=19)
    assert report == compare_saved_runs(left, right, bootstrap_samples=1000, seed=19)
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    for metric in report["paired_conservative_pass_hat_ks"].values():
        assert metric["paired_tasks"] == 2
        assert metric["difference_left_minus_right"] == 0.5
        assert metric["percentile_interval"] == [0.0, 1.0]
    assert report["paired_latency_seconds"]["percentile_interval"] == [1.0, 1.0]
    assert (
        report["paired_cost_usd_per_scheduled_conversation"]["difference_left_minus_right"] == 0.5
    )
    assert report["per_task"][0]["left"]["trials"][0]["trial"] == 0
    assert not report["left"]["provenance"]["saved_artifact_binding_validated"]
    assert PRIVATE not in json.dumps(report)
    assert report["fresh_judge_calls"] == report["writes_performed"] == 0


def test_bootstrap_resamples_paired_task_differences_and_never_claims_one_task_precision():
    result = paired_task_interval([1.0, 0.5], [0.5, 0.0], samples=200, seed=11)
    assert result["percentile_interval"] == [0.5, 0.5]
    assert paired_task_interval([1.0], [0.0], samples=200, seed=11)["percentile_interval"] is None


def test_more_than_four_trials_uses_combinatorial_task_statistic(tmp_path):
    left = _fixture(tmp_path, "safe_agent", overrides={"num_trials": 5})
    right = _fixture(tmp_path, "llm_agent", overrides={"num_trials": 5})
    _change(
        left,
        "results.json",
        lambda value: [
            row.update(reward_info={"reward": float(row["task_id"] == "b" or row["trial"] < 4)})
            for row in value["simulations"]
        ],
    )
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    assert report["paired_conservative_pass_hat_ks"]["1"]["left_mean"] == 0.9
    assert report["paired_conservative_pass_hat_ks"]["4"]["left_mean"] == 0.6


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_agent": "different-agent-model"},
        {"llm_user": "different-user-model"},
        {"llm_args_agent": {"temperature": 0.5}},
        {"seed": 301},
        {"retrieval_config_kwargs": {"top_k": 7}},
    ],
)
def test_different_frozen_conditions_cannot_form_a_comparison(tmp_path, overrides):
    left = _fixture(tmp_path, "safe_agent")
    right = _fixture(tmp_path, "llm_agent", overrides=overrides)
    with pytest.raises(ComparisonError, match="CONDITION_MISMATCH"):
        compare_saved_runs(left, right)


@pytest.mark.parametrize("field", ["judge_args", "source_hash", "domain_policy"])
def test_evaluator_source_and_recorded_policy_mismatches_are_rejected(tmp_path, field):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    if field == "judge_args":
        _change(right, "manifest.json", lambda value: value.update(judge_args={"temperature": 0.7}))
    elif field == "source_hash":
        _change(
            right,
            "manifest.json",
            lambda value: value["inventory"].update(task_content_sha256="e" * 64),
        )
    else:
        _change(
            right,
            "results.json",
            lambda value: value["info"]["environment_info"].update(policy="changed policy"),
        )
    with pytest.raises(ComparisonError, match="MISMATCH"):
        compare_saved_runs(left, right)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[0].update(seed=123),
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].update(task_id="not_scheduled"),
        lambda rows: rows[0].update(duration=-1),
        lambda rows: rows[0].update(reward_info={"reward": True}),
    ],
)
def test_ambiguous_unmatched_or_invalid_recorded_results_cannot_enter_intervals(tmp_path, mutation):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _change(right, "results.json", lambda value: mutation(value["simulations"]))
    with pytest.raises(ComparisonError):
        compare_saved_runs(left, right)


def test_partial_runs_keep_conservative_denominators_and_drop_incomplete_resource_pairs(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _invoice(left)

    def remove_and_fail(value):
        value["simulations"].pop()
        value["simulations"][-1].update(termination_reason="infrastructure_error", reward_info=None)

    _change(right, "results.json", remove_and_fail)
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    assert report["denominators"]["scheduled_pairs_per_system"] == 8
    assert report["right"]["report"]["saved_reward_metrics"]["eligible_attempts"] == 6
    assert report["paired_conservative_pass_hat_ks"]["4"]["paired_tasks"] == 2
    assert report["resource_comparison_missingness"]["latency_excluded_tasks"] == 1
    assert report["resource_comparison_missingness"]["cost_excluded_tasks"] == 1
    assert report["denominators"]["official_difference_interval"] is None


def test_known_run_invoice_without_allocations_does_not_invent_per_task_costs(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _invoice(left, allocate=False)
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    assert report["left"]["report"]["costs"]["known_total_usd"] == "10.00"
    assert report["left"]["report"]["costs"]["complete"]
    assert report["paired_cost_usd_per_scheduled_conversation"]["paired_tasks"] == 0
    assert (
        report["paired_cost_usd_per_scheduled_conversation"]["difference_left_minus_right"] is None
    )


def test_call_counts_distinguish_safe_attempt_usage_stock_responses_and_user_tools(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")

    def add_observations(value):
        value["simulations"][0]["messages"] = [
            {
                "role": "assistant",
                "tool_calls": [{"name": PRIVATE}],
                "usage": {"safe_model_attempts": 2, "safe_usage_source": [PRIVATE]},
            },
            {"role": "tool", "requestor": "assistant", "content": PRIVATE},
            {"role": "user", "tool_calls": [{"name": PRIVATE}]},
            {"role": "tool", "requestor": "user", "content": PRIVATE},
            {
                "role": "tool",
                "tool_messages": [
                    {"requestor": "assistant", "content": PRIVATE},
                    {"requestor": "user", "content": PRIVATE},
                ],
            },
            {"role": "assistant", "content": PRIVATE},
        ]
        value["simulations"][-1]["messages"] = None

    _change(left, "results.json", add_observations)
    _change(
        right,
        "results.json",
        lambda value: value["simulations"][0].update(
            messages=[{"role": "assistant", "content": PRIVATE}]
        ),
    )
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    calls = report["per_task"][0]["left"]["calls"]
    assert calls["recorded_assistant_messages"] == 2
    assert calls["recorded_emitted_agent_tool_calls"] == 1
    assert calls["recorded_agent_tool_results"] == 2
    assert calls["safe_model_attempts_recorded"] == 2
    assert calls["safe_assistant_messages_without_attempt_usage"] == 1
    assert report["per_task"][1]["left"]["calls"]["trials_missing_message_records"] == 1
    stock = report["per_task"][0]["right"]["calls"]
    assert stock["recorded_assistant_messages"] == 1
    assert stock["safe_model_attempts_recorded"] is stock["actual_provider_attempts"] is None
    assert report["left"]["report"]["safe_inference_recorded_usage"]["sources"] == {
        "unrecognized": 1
    }
    assert PRIVATE not in json.dumps(report)


def test_reviewed_failure_taxonomy_is_bound_to_the_actual_non_success_record(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    row = json.loads((right / "results.json").read_text())["simulations"][0]
    annotation = {
        "run_id": "authored_llm_agent",
        "task_id": "a",
        "trial": 0,
        "simulation_sha256": content_hash(row),
        "label": "reasoning_error",
        "reviewer": PRIVATE,
    }
    path = tmp_path / "reviews.json"
    _write(path, {"schema_version": 1, "annotations": [annotation]})
    report = compare_saved_runs(left, right, bootstrap_samples=100, failure_annotations=path)
    reviewed = report["reviewed_failure_taxonomy"]["by_run"]["authored_llm_agent"]
    assert reviewed["labels"] == {"reasoning_error": 1}
    assert reviewed["unreviewed_recorded_non_successes"] == 7
    assert PRIVATE not in json.dumps(report)
    _write(path, {"schema_version": 1, "annotations": [annotation, annotation]})
    with pytest.raises(ComparisonError, match="DUPLICATE"):
        compare_saved_runs(left, right, failure_annotations=path)
    _write(
        path, {"schema_version": 1, "annotations": [dict(annotation, simulation_sha256="f" * 64)]}
    )
    with pytest.raises(ComparisonError, match="BINDING"):
        compare_saved_runs(left, right, failure_annotations=path)


def test_saved_artifact_binding_rejects_later_changes_to_results(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    for path in (left, right):
        _write(path / "official_metrics.json", {"avg_reward": 0.5})
        bindings = {
            name: hashlib.sha256((path / name).read_bytes()).hexdigest()
            for name in ("results.json", "official_metrics.json")
        }
        _change(
            path,
            "manifest.json",
            lambda value, bindings=bindings: value.update(saved_artifacts_sha256=bindings),
        )
    assert compare_saved_runs(left, right, bootstrap_samples=100)["left"]["provenance"][
        "saved_artifact_binding_validated"
    ]
    _change(
        right,
        "results.json",
        lambda value: value["simulations"][0].update(reward_info={"reward": 1.0}),
    )
    with pytest.raises(ComparisonError, match="ARTIFACT_HASH"):
        compare_saved_runs(left, right)


def test_completed_final_run_requires_saved_artifact_binding(tmp_path):
    left = _fixture(tmp_path, "safe_agent", full_final=True)
    right = _fixture(tmp_path, "llm_agent", full_final=True)
    with pytest.raises(ComparisonError, match="FINAL_SAVED_ARTIFACT_BINDING_REQUIRED"):
        compare_saved_runs(left, right)
