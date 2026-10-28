"""Read-only matched-run comparison from recorded evidence, with no fresh judging.

The task is the paired bootstrap unit; all its trials stay together. Intervals
describe task variability conditional on these recorded model/simulator runs.
They do not estimate all model randomness or certify generalization/security.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from . import EXPECTED_TASK_COUNTS, TAU_COMMIT
from .config import BenchmarkConfig, harness_model_settings
from .execution import load_episode_index
from .inventory import content_hash, pinned_source_manifest
from .protocol import BoundaryModel
from .reporting import _reward, _success, cost_accounting, pass_hat_k, summarize_saved_results

Pair = tuple[str, int]
MAX_INPUT_BYTES = 256 * 1024 * 1024
MATCHED_CONFIG_FIELDS = (
    "tau_commit",
    "domain",
    "stage",
    "task_split_name",
    "num_trials",
    "seed",
    "max_steps",
    "max_errors",
    "max_concurrency",
    "timeout",
    "max_retries",
    "retrieval_config",
    "retrieval_config_kwargs",
    "model_route",
    "local_api_base",
    "local_timing",
    "stop_after_infrastructure_errors",
    "llm_agent",
    "llm_args_agent",
    "llm_user",
    "llm_args_user",
)
TERMINATIONS = frozenset(
    {
        "user_stop",
        "agent_stop",
        "max_steps",
        "timeout",
        "too_many_errors",
        "agent_error",
        "user_error",
        "infrastructure_error",
        "context_window_exceeded",
        "unexpected_error",
    }
)
FailureLabel = Literal[
    "reasoning_error",
    "retrieval_error",
    "tool_argument_error",
    "policy_violation",
    "false_refusal",
    "infrastructure_failure",
    "budget_exhaustion",
    "uncertain_outcome",
    "other_reviewed",
]


class ComparisonError(ValueError):
    """A fixed failure code; never include private transcript or credential text."""


def _verified_preflight(
    manifest: dict[str, Any] | None, config: BenchmarkConfig
) -> dict[str, Any] | None:
    """Use recorded launch evidence only when its saved binding is valid.

    Older manifests may omit preflight evidence or its hash. Unbound evidence
    does not establish an effective runtime default. A supplied binding must
    validate even when the explicit run configuration would otherwise suffice.
    """
    if manifest is None:
        return None
    preflight = manifest.get("preflight")
    digest = manifest.get("preflight_sha256")
    if preflight is None:
        _require(digest is None, "PREFLIGHT_HASH_MISMATCH")
        return None
    _require(isinstance(preflight, dict), "PREFLIGHT_METADATA_INVALID")
    if digest is None:
        return None
    _require(digest == content_hash(preflight), "PREFLIGHT_HASH_MISMATCH")
    _require(
        preflight.get("schema_version") == "safe_agent.tau3_preflight.v1"
        and preflight.get("kind") == "benchmark_preflight"
        and preflight.get("config_sha256") == manifest["config_sha256"]
        and preflight.get("run_id") == config.run_id
        and preflight.get("safe_revision") == manifest["safe_revision"]
        and preflight.get("configuration_validated") is True
        and preflight.get("ready_for_attempt") is True,
        "PREFLIGHT_RUN_BINDING_MISMATCH",
    )
    return preflight


def _agent_transport_controls(
    config: BenchmarkConfig, *, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Recorded declarations for the frozen benchmark, not measured attempts.

    The pinned tau2/config.py has DEFAULT_MAX_RETRIES = 3; llm_utils.py
    applies it when num_retries is absent or null. The source manifest is
    verified when saved runs are loaded. SAFE's revision is separately recorded;
    never substitute the installed RuntimeLimits defaults for its saved evidence.
    """
    preflight = _verified_preflight(manifest, config)
    if config.system == "safe_agent":
        from safe_agent.contracts.episode_inference import validate_episode_model_arguments

        tokens = config.episode_budget.get("max_output_tokens")
        if "max_output_tokens" in config.episode_budget:
            _require(type(tokens) is int and tokens > 0, "INVALID_SAFE_TRANSPORT_CONTROLS")
        basis = ("explicit_episode_budget" if tokens is not None
                 else "legacy_runtime_default_not_established")
        if preflight is not None:
            limits = preflight.get("effective_safe_limits")
            episode = limits.get("episode_budget") if isinstance(limits, dict) else None
            recorded = episode.get("max_output_tokens") if isinstance(episode, dict) else None
            _require(
                preflight.get("safe_budget_applies") is True
                and type(recorded) is int and recorded > 0,
                "PREFLIGHT_SAFE_OUTPUT_CAP_INVALID",
            )
            _require(tokens is None or tokens == recorded, "PREFLIGHT_SAFE_OUTPUT_CAP_MISMATCH")
            tokens = recorded
            basis = "recorded_preflight_runtime_budget"
        alias = config.llm_args_agent.get("max_tokens")
        if "max_tokens" in config.llm_args_agent:
            _require(type(alias) is int and alias > 0, "INVALID_SAFE_TRANSPORT_CONTROLS")
        try:
            # An alias alone does not establish the historical runtime budget.
            # Its shape is still checked; equality is checked when a cap is known.
            validate_episode_model_arguments(
                config.llm_args_agent,
                max_output_tokens=(tokens if isinstance(tokens, int)
                                   else alias if isinstance(alias, int) else 0),
            )
        except (ValueError, TypeError):
            raise ComparisonError("INVALID_SAFE_TRANSPORT_CONTROLS") from None
        controls = {
            "provider_num_retries": 0,
            "max_output_tokens": tokens,
            "output_cap_basis": basis,
            "inference_timing": _recorded_timing(config, manifest, preflight),
        }
        _check_preflight_transport(preflight, controls)
        return controls
    _, arguments = harness_model_settings(config, "agent")
    retries = arguments.get("num_retries")
    retries = 3 if retries is None else retries
    tokens = arguments.get("max_tokens")
    if type(retries) is not int or not 0 <= retries <= 20:
        raise ComparisonError("INVALID_STOCK_TRANSPORT_CONTROLS")
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ComparisonError("INVALID_STOCK_TRANSPORT_CONTROLS")
    controls = {
        "provider_num_retries": retries,
        "max_output_tokens": tokens,
        "output_cap_basis": "explicit_harness_argument" if tokens is not None
        else "provider_default_not_established",
        "inference_timing": _recorded_timing(config, manifest, preflight),
    }
    _check_preflight_transport(preflight, controls)
    return controls


def _recorded_timing(
    config: BenchmarkConfig,
    manifest: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
) -> dict[str, Any]:
    """Never apply today's timeout defaults to historical measurements."""
    from safe_agent.contracts.episode_inference import LocalTimingProfile

    raw = manifest.get("config", {}) if manifest is not None else config.model_dump()
    profile = raw.get("local_timing")
    timing = None
    basis = "historical_defaults_not_recorded"
    if profile is not None:
        try:
            validated = LocalTimingProfile.model_validate(profile)
        except (TypeError, ValueError):
            raise ComparisonError("RECORDED_TIMING_PROFILE_INVALID") from None
        timing = {
            name: getattr(validated, name)
            for name in ("provider_timeout_seconds", "transport_timeout_seconds", "request_timeout_seconds")
        }
        timing.update(
            harness_provider_timeout_seconds=validated.provider_timeout_seconds,
            absolute_local_deadline_enforced=True,
        )
        basis = "explicit_saved_local_profile"
    recorded = preflight.get("effective_inference_timing") if preflight is not None else None
    if recorded is not None:
        _require(isinstance(recorded, dict), "PREFLIGHT_TIMING_INVALID")
        for name in ("provider_timeout_seconds", "transport_timeout_seconds", "request_timeout_seconds", "harness_provider_timeout_seconds"):
            value = recorded.get(name)
            _require(
                value is None or (type(value) in {int, float} and math.isfinite(value) and value > 0),
                "PREFLIGHT_TIMING_INVALID",
            )
        _require(
            type(recorded.get("absolute_local_deadline_enforced")) is bool,
            "PREFLIGHT_TIMING_INVALID",
        )
        if timing is not None:
            _require(all(recorded.get(name) == value for name, value in timing.items()), "PREFLIGHT_TIMING_MISMATCH")
        timing = recorded
        basis = "hash_bound_saved_preflight"
    timing = timing or {}
    harness = timing.get("harness_provider_timeout_seconds")
    safe = config.system == "safe_agent"
    return {
        "basis": basis,
        "agent_provider_timeout_seconds": timing.get("provider_timeout_seconds") if safe else harness,
        "user_provider_timeout_seconds": harness,
        "safe_transport_timeout_seconds": timing.get("transport_timeout_seconds") if safe else None,
        "safe_request_timeout_seconds": timing.get("request_timeout_seconds") if safe else None,
        "absolute_local_deadline_enforced": timing.get("absolute_local_deadline_enforced"),
        "simulation_timeout_seconds": raw.get("timeout"),
        "simulation_timeout_recorded": "timeout" in raw,
        "measured_timeout_behavior": False,
    }


def _check_preflight_transport(
    preflight: dict[str, Any] | None, controls: dict[str, Any]
) -> None:
    if preflight is None or "model_roles" not in preflight:
        return
    roles = preflight["model_roles"]
    agent = roles.get("agent") if isinstance(roles, dict) else None
    if not isinstance(agent, dict):
        raise ComparisonError("PREFLIGHT_AGENT_CONTROLS_INVALID")
    # Earlier preflight records did not contain output caps. When a control is
    # present, do not silently replace conflicting saved evidence.
    for name in ("provider_num_retries", "max_output_tokens"):
        if name in agent:
            _require(
                type(agent[name]) is type(controls[name]) and agent[name] == controls[name],
                "PREFLIGHT_AGENT_CONTROLS_MISMATCH",
            )


def _transport_comparability(controls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    left, right = controls["left"], controls["right"]
    retry_match = left["provider_num_retries"] == right["provider_num_retries"]
    output_match = (
        left["max_output_tokens"] is not None
        and right["max_output_tokens"] is not None
        and left["max_output_tokens"] == right["max_output_tokens"]
    )
    timing_left, timing_right = left["inference_timing"], right["inference_timing"]
    timing_fields = ("agent_provider_timeout_seconds", "user_provider_timeout_seconds", "absolute_local_deadline_enforced")
    timing_known = all(timing_left[name] is not None and timing_right[name] is not None for name in timing_fields)
    timing_match = all(timing_left[name] == timing_right[name] for name in timing_fields) if timing_known else None
    return {
        "provider_retry_limits_match": retry_match,
        "explicit_output_caps_match": output_match,
        "agent_transport_controls_match": retry_match and output_match,
        "agent_transport_controls_match_scope": "declared provider retries and output caps; timing is reported separately",
        "provider_and_user_timing_match": timing_match,
        "agent_transport_and_timing_match": (retry_match and output_match and timing_match) if timing_known else None,
        "timing_evidence_complete": timing_known,
        "timing_basis": "saved explicit configuration or hash-bound preflight only; historical missing values stay unknown",
        "safe_only_deadlines_remain_system_specific": True,
        "actual_provider_attempts_measured": False,
        "interpretation": (
            "Full-system comparison. Equal declared retries and output caps do not "
            "isolate enforcement overhead: prompts, planning, tool projection, "
            "repair behavior and SAFE budgets remain system-specific."
        ),
    }


class FailureAnnotation(BoundaryModel):
    run_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    trial: int = Field(ge=0)
    simulation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: FailureLabel
    reviewer: str = Field(min_length=1, max_length=200)


class FailureReview(BoundaryModel):
    schema_version: Literal[1]
    annotations: list[FailureAnnotation] = Field(max_length=32768)


@dataclass
class _SavedRun:
    manifest: dict[str, Any]
    config: BenchmarkConfig
    rows: dict[Pair, dict[str, Any]]
    scheduled: list[Pair]
    summary: dict[str, Any]
    pair_costs: dict[tuple[str, int | None], dict[str, Any]]
    info: dict[str, Any] | None
    official_metrics: dict[str, Any] | None
    provenance: dict[str, Any]


def _require(value: bool, code: str) -> None:
    if not value:
        raise ComparisonError(code)


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ComparisonError("NONFINITE_JSON_VALUE")


def _read_json(path: Path) -> tuple[Any, str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    _require(len(raw) <= MAX_INPUT_BYTES, "COMPARISON_INPUT_TOO_LARGE")
    return (
        json.loads(raw, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant),
        hashlib.sha256(raw).hexdigest(),
    )


def _recorded_metadata(manifest: dict[str, Any], config: BenchmarkConfig) -> None:
    _require(manifest.get("kind") == "official_tau3_text", "OFFICIAL_RUN_REQUIRED")
    _require(manifest.get("tau_commit") == TAU_COMMIT, "BENCHMARK_REVISION_MISMATCH")
    _require(
        manifest.get("config_sha256") == content_hash(manifest["config"]), "CONFIG_HASH_MISMATCH"
    )
    _require(manifest.get("run_id") == config.run_id, "CONFIG_RUN_ID_MISMATCH")
    _require(manifest.get("system") == config.system, "CONFIG_SYSTEM_MISMATCH")
    expected_mode = "canonical_safe" if config.system == "safe_agent" else "stock_baseline"
    _require(manifest.get("runtime_mode") == expected_mode, "CANONICAL_CONDITION_REQUIRED")
    _require(
        manifest.get("evaluation_type") == "ALL"
        and manifest.get("automatic_review") is False
        and type(manifest.get("retries")) is int
        and manifest["retries"] == 0,
        "EVALUATOR_CONDITION_INVALID",
    )
    _require(
        isinstance(manifest.get("judge_model"), str) and bool(manifest["judge_model"]),
        "JUDGE_METADATA_MISSING",
    )
    _require(isinstance(manifest.get("judge_args"), dict), "JUDGE_METADATA_MISSING")
    inventory = manifest["inventory"]
    source = inventory["source_verification"]
    pinned = pinned_source_manifest()
    expected_files = sum(
        path.startswith("src/tau2/")
        or path == "pyproject.toml"
        or path.startswith(f"data/tau2/domains/{config.domain}/")
        for path in pinned["files"]
    )
    _require(
        inventory.get("source_verified") is True
        and inventory.get("status") == "ready"
        and inventory.get("commit") == TAU_COMMIT
        and inventory.get("domain") == config.domain
        and source.get("verified") is True
        and source.get("manifest_sha256") == content_hash(pinned)
        and source.get("required_files") == source.get("verified_files") == expected_files
        and all(
            source.get(name) == []
            for name in ("missing_files", "modified_files", "unsafe_paths", "unexpected_task_files")
        ),
        "PINNED_SOURCE_VERIFICATION_REQUIRED",
    )
    ids = inventory["selected_task_ids"]
    _require(
        isinstance(ids, list)
        and bool(ids)
        and all(isinstance(item, str) and item for item in ids)
        and len(ids) == len(set(ids))
        and inventory.get("selected_task_count") == len(ids)
        and inventory.get("task_ids_sha256") == content_hash(ids)
        and re.fullmatch(r"[0-9a-f]{64}", inventory.get("task_content_sha256", "")) is not None,
        "TASK_INVENTORY_INVALID",
    )
    expected_schedule = [
        {"task_id": task, "trial": trial} for task in ids for trial in range(config.num_trials)
    ]
    _require(
        manifest["scheduled"] == expected_schedule and len(expected_schedule) <= 16384,
        "TASK_TRIAL_SCHEDULE_INVALID",
    )
    _require(
        inventory.get("domain_task_count") == EXPECTED_TASK_COUNTS[config.domain],
        "DOMAIN_INVENTORY_COUNT_MISMATCH",
    )
    if config.stage in {"coverage", "final"}:
        _require(len(ids) == EXPECTED_TASK_COUNTS[config.domain], "FULL_DOMAIN_SCHEDULE_REQUIRED")
    if config.model_route == "local":
        _require(
            type(inventory.get("selected_tasks_with_nl_assertions")) is int
            and inventory["selected_tasks_with_nl_assertions"] == 0,
            "LOCAL_ZERO_NL_ASSERTION_SCHEDULE_REQUIRED",
        )
    official = manifest["official_config"]
    for name in (
        "domain",
        "num_trials",
        "seed",
        "max_steps",
        "max_errors",
        "max_concurrency",
        "max_retries",
        "timeout",
    ):
        _require(
            name in official and official[name] == getattr(config, name), "OFFICIAL_CONFIG_MISMATCH"
        )
    for role in ("agent", "user"):
        model, arguments = harness_model_settings(config, role)
        _require(
            official.get("llm_" + role) == model
            and official.get("llm_args_" + role) == arguments,
            "OFFICIAL_CONFIG_MISMATCH",
        )
    _require(
        official.get("agent")
        == ("safe_agent_" + config.run_id if config.system == "safe_agent" else "llm_agent")
        and official.get("user") == "user_simulator"
        and official.get("auto_review") is False
        and official.get("auto_resume") is False
        and official.get("enforce_communication_protocol") is True,
        "OFFICIAL_PROTOCOL_MISMATCH",
    )
    if config.domain == "banking_knowledge":
        _require(
            official.get("retrieval_config") == "bm25"
            and official.get("retrieval_config_kwargs") == config.retrieval_config_kwargs,
            "RETRIEVAL_CONFIG_MISMATCH",
        )
    revision = manifest["safe_revision"]
    _require(
        re.fullmatch(r"[0-9a-f]{40}", revision.get("commit", "")) is not None
        and re.fullmatch(r"[0-9a-f]{64}", revision.get("runtime_sources_sha256", "")) is not None,
        "RUNTIME_REVISION_REQUIRED",
    )
    if config.stage == "final":
        _require(revision.get("dirty") is False, "FINAL_RUNTIME_MUST_BE_CLEAN")
    _require(
        isinstance(manifest.get("dependencies"), dict) and bool(manifest["dependencies"]),
        "DEPENDENCY_METADATA_REQUIRED",
    )


def _validate_info(info: dict[str, Any], manifest: dict[str, Any], config: BenchmarkConfig) -> None:
    for name in ("num_trials", "seed", "max_steps", "max_errors"):
        _require(info.get(name) == getattr(config, name), "RESULTS_INFO_MISMATCH")
    for role in ("agent", "user"):
        recorded = info[role + "_info"]
        model, arguments = harness_model_settings(config, role)
        _require(
            recorded.get("llm") == model
            and recorded.get("llm_args") == arguments
            and recorded.get("implementation") == manifest["official_config"][role],
            "RESULTS_MODEL_MISMATCH",
        )
    environment = info["environment_info"]
    _require(
        environment.get("domain_name") == config.domain
        and isinstance(environment.get("policy"), str)
        and bool(environment["policy"])
        and isinstance(info["user_info"].get("global_simulation_guidelines"), str)
        and bool(info["user_info"]["global_simulation_guidelines"]),
        "RESULTS_POLICY_METADATA_REQUIRED",
    )
    if config.domain == "banking_knowledge":
        _require(
            info.get("retrieval_config") == "bm25"
            and info.get("retrieval_config_kwargs") == config.retrieval_config_kwargs,
            "RESULTS_RETRIEVAL_MISMATCH",
        )


def _load(run_dir: Path) -> _SavedRun:
    manifest, manifest_hash = _read_json(run_dir / "manifest.json")
    config = BenchmarkConfig.model_validate(manifest["config"])
    _recorded_metadata(manifest, config)
    scheduled = [(row["task_id"], row["trial"]) for row in manifest["scheduled"]]
    results_path = run_dir / "results.json"
    results, result_hash = (
        _read_json(results_path) if results_path.exists() else ({"simulations": []}, None)
    )
    simulations = results["simulations"]
    _require(isinstance(simulations, list), "RESULTS_ARRAY_REQUIRED")
    info = results.get("info")
    if result_hash is not None:
        if not isinstance(info, dict):
            raise ComparisonError("RESULTS_INFO_REQUIRED")
        _validate_info(info, manifest, config)
    seeds_rng = random.Random(config.seed)
    seeds = [seeds_rng.randint(0, 1000000) for _ in range(config.num_trials)]
    rows: dict[Pair, dict[str, Any]] = {}
    for row in simulations:
        pair = (row["task_id"], row["trial"])
        _require(
            type(pair[1]) is int and pair in scheduled and pair not in rows,
            "DUPLICATE_OR_UNEXPECTED_RESULT_PAIR",
        )
        _require(
            type(row.get("seed")) is int and row["seed"] == seeds[pair[1]],
            "RECORDED_TRIAL_SEED_MISMATCH",
        )
        _require(row.get("termination_reason") in TERMINATIONS, "RESULT_TERMINATION_INVALID")
        _require(
            row.get("mode", "half_duplex") == "half_duplex"
            and row.get("hallucination_retries_used", 0) == 0,
            "TEXT_WITHOUT_RETRIED_TRAJECTORIES_REQUIRED",
        )
        reward = _reward(row)
        _require(
            reward is None
            or (type(reward) in {int, float} and math.isfinite(reward) and 0 <= reward <= 1),
            "RECORDED_REWARD_INVALID",
        )
        duration = row.get("duration")
        _require(
            duration is None
            or (type(duration) in {int, float} and math.isfinite(duration) and duration >= 0),
            "RECORDED_DURATION_INVALID",
        )
        rows[pair] = row
    additional_path = run_dir / "additional_costs.jsonl"
    additional: list[dict[str, Any]] = []
    costs_hash = None
    if additional_path.exists():
        with additional_path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        _require(len(raw) <= MAX_INPUT_BYTES, "COMPARISON_INPUT_TOO_LARGE")
        costs_hash = hashlib.sha256(raw).hexdigest()
        additional = [
            json.loads(line, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant)
            for line in raw.splitlines()
            if line.strip()
        ]
    summary = summarize_saved_results(
        manifest, simulations, additional,
        episode_index=load_episode_index(run_dir, manifest, simulations),
    )
    _, pair_costs = cost_accounting(manifest, simulations, additional)
    metrics_path = run_dir / "official_metrics.json"
    official, metrics_hash = _read_json(metrics_path) if metrics_path.exists() else (None, None)
    bindings = manifest.get("saved_artifacts_sha256")
    if config.stage == "final" and manifest.get("status") == "complete":
        _require(bindings is not None, "FINAL_SAVED_ARTIFACT_BINDING_REQUIRED")
    if bindings is not None:
        _require(
            isinstance(bindings, dict)
            and set(bindings) == {"results.json", "official_metrics.json"}
            and result_hash is not None
            and metrics_hash is not None
            and bindings["results.json"] == result_hash
            and bindings["official_metrics.json"] == metrics_hash,
            "SAVED_ARTIFACT_HASH_MISMATCH",
        )
    if official is not None:
        _require(isinstance(official, dict), "OFFICIAL_METRICS_INVALID")
        official = {
            key: official[key]
            for key in (
                "avg_reward",
                "pass_hat_ks",
                "total_simulations",
                "total_tasks",
                "infra_error_count",
                "avg_agent_cost",
            )
            if key in official
        }
        for name, value in official.items():
            if name == "pass_hat_ks":
                _require(isinstance(value, dict), "OFFICIAL_METRICS_INVALID")
                for k, score in value.items():
                    _require(
                        isinstance(k, str)
                        and k.isdigit()
                        and 1 <= int(k) <= config.num_trials
                        and type(score) in {int, float}
                        and math.isfinite(score)
                        and 0 <= score <= 1,
                        "OFFICIAL_METRICS_INVALID",
                    )
            else:
                _require(
                    type(value) in {int, float} and math.isfinite(value) and value >= 0,
                    "OFFICIAL_METRICS_INVALID",
                )
    return _SavedRun(
        manifest,
        config,
        rows,
        scheduled,
        summary,
        pair_costs,
        info,
        official,
        {
            "manifest_sha256": manifest_hash,
            "results_sha256": result_hash,
            "additional_costs_sha256": costs_hash,
            "official_metrics_sha256": metrics_hash,
            "saved_artifact_binding_validated": bindings is not None,
            "model_route_defaulted_from_legacy_config": "model_route" not in manifest["config"],
            "origin_authenticity_verified": False,
        },
    )


def _match(left: _SavedRun, right: _SavedRun) -> None:
    _require(left.config.run_id != right.config.run_id, "DISTINCT_RUN_IDS_REQUIRED")
    _require(
        {left.config.system, right.config.system} == {"safe_agent", "llm_agent"},
        "SAFE_AND_STOCK_PAIR_REQUIRED",
    )
    for name in MATCHED_CONFIG_FIELDS:
        _require(
            getattr(left.config, name) == getattr(right.config, name),
            "CONDITION_MISMATCH_" + name.upper(),
        )
    _require(left.scheduled == right.scheduled, "SCHEDULE_MISMATCH")
    for name in (
        "judge_model",
        "judge_args",
        "evaluation_type",
        "automatic_review",
        "retries",
        "safe_revision",
        "dependencies",
    ):
        _require(left.manifest[name] == right.manifest[name], "METADATA_MISMATCH_" + name.upper())
    for name in ("task_content_sha256", "task_ids_sha256", "source_verification", "tool_inventory"):
        _require(
            left.manifest["inventory"][name] == right.manifest["inventory"][name],
            "SOURCE_MISMATCH_" + name.upper(),
        )

    def shared_official(run: _SavedRun) -> dict[str, Any]:
        # Each role's exact effective routing was validated above. Local SAFE
        # uses its sidecar, while stock uses LiteLLM's explicit loopback route;
        # compare their already-matched logical agent settings here.
        return {
            key: getattr(run.config, key) if key in {"llm_agent", "llm_args_agent"} else value
            for key, value in run.manifest["official_config"].items()
            if key not in {"agent", "save_to"}
        }

    _require(shared_official(left) == shared_official(right), "OFFICIAL_CONDITION_MISMATCH")
    if left.info is not None and right.info is not None:
        for name in (
            "environment_info",
            "user_info",
            "text_streaming_config",
            "speech_complexity",
            "audio_native_config",
        ):
            _require(left.info.get(name) == right.info.get(name), "RECORDED_CONTEXT_MISMATCH")
    for pair in set(left.rows) & set(right.rows):
        _require(left.rows[pair]["seed"] == right.rows[pair]["seed"], "PAIRED_SEED_MISMATCH")
        _require(
            left.rows[pair].get("policy") == right.rows[pair].get("policy"),
            "RECORDED_POLICY_MISMATCH",
        )


def _percentile(sorted_values: list[float], probability: float) -> float:
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def paired_task_interval(
    left: list[float], right: list[float], *, samples: int, seed: int
) -> dict[str, Any]:
    """Paired task-cluster percentile bootstrap, with fixed PRNG and finite bounds."""
    _require(len(left) == len(right), "PAIRED_STATISTIC_LENGTH_MISMATCH")
    _require(
        type(samples) is int and 100 <= samples <= 10000 and type(seed) is int,
        "BOOTSTRAP_SETTINGS_INVALID",
    )
    differences = [a - b for a, b in zip(left, right, strict=True)]
    _require(all(math.isfinite(value) for value in [*left, *right]), "BOOTSTRAP_STATISTIC_INVALID")
    count = len(differences)
    interval = None
    if count >= 2:
        rng = random.Random(seed)
        draws = sorted(
            sum(differences[rng.randrange(count)] for _ in range(count)) / count
            for _ in range(samples)
        )
        interval = [_percentile(draws, 0.025), _percentile(draws, 0.975)]
    return {
        "paired_tasks": count,
        "left_mean": sum(left) / count if count else None,
        "right_mean": sum(right) / count if count else None,
        "difference_left_minus_right": sum(differences) / count if count else None,
        "confidence_level": 0.95,
        "percentile_interval": interval,
        "interval_unavailable_reason": "fewer_than_two_paired_tasks" if count < 2 else None,
        "bootstrap_samples": samples,
        "bootstrap_seed": seed,
    }


def _outcome(row: dict[str, Any] | None) -> str:
    if row is None:
        return "missing"
    if row["termination_reason"] == "infrastructure_error":
        return "infrastructure_error"
    if _reward(row) is None:
        return "ungraded"
    return "success" if _success(_reward(row)) else "unsuccessful"


def _failure_reviews(path: Path | None, runs: tuple[_SavedRun, _SavedRun]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {run.config.run_id: Counter() for run in runs}
    reviewed: set[tuple[str, str, int]] = set()
    provenance = None
    if path is not None:
        value, provenance = _read_json(path)
        document = FailureReview.model_validate(value)
        by_run = {run.config.run_id: run for run in runs}
        for item in document.annotations:
            key = (item.run_id, item.task_id, item.trial)
            _require(
                item.run_id in by_run and key not in reviewed,
                "FAILURE_REVIEW_RUN_OR_DUPLICATE_INVALID",
            )
            row = by_run[item.run_id].rows.get((item.task_id, item.trial))
            _require(
                row is not None
                and _outcome(row) != "success"
                and content_hash(row) == item.simulation_sha256,
                "FAILURE_REVIEW_BINDING_INVALID",
            )
            reviewed.add(key)
            counts[item.run_id][item.label] += 1
    return {
        "basis": "one human-reviewed primary label per hash-bound recorded non-success; no automatic semantic classification",
        "annotations_sha256": provenance,
        "by_run": {
            run.config.run_id: {
                "reviewed": sum(counts[run.config.run_id].values()),
                "labels": dict(sorted(counts[run.config.run_id].items())),
                "unreviewed_recorded_non_successes": sum(
                    _outcome(row) != "success" for row in run.rows.values()
                )
                - sum(counts[run.config.run_id].values()),
            }
            for run in runs
        },
    }


def _call_counts(row: dict[str, Any] | None, system: str) -> dict[str, Any]:
    messages = row.get("messages") if row is not None else None
    counts: Counter[str] = Counter()
    if messages is not None:
        _require(isinstance(messages, list), "RECORDED_MESSAGES_INVALID")
        for message in messages:
            _require(isinstance(message, dict), "RECORDED_MESSAGES_INVALID")
            if message.get("role") == "assistant":
                counts["assistant_messages"] += 1
                calls = message.get("tool_calls") or []
                _require(
                    isinstance(calls, list) and all(isinstance(call, dict) for call in calls),
                    "RECORDED_TOOL_CALLS_INVALID",
                )
                counts["emitted_agent_tool_calls"] += len(calls)
                if system == "safe_agent":
                    usage = message.get("usage") or {}
                    _require(isinstance(usage, dict), "RECORDED_USAGE_INVALID")
                    attempts = usage.get("safe_model_attempts")
                    if type(attempts) is int and attempts >= 0:
                        counts["safe_model_attempts"] += attempts
                        counts["assistant_messages_with_attempt_usage"] += 1
                    else:
                        counts["assistant_messages_without_attempt_usage"] += 1
            elif message.get("role") == "tool":
                results = message.get("tool_messages", [message])
                _require(
                    isinstance(results, list)
                    and all(isinstance(result, dict) for result in results),
                    "RECORDED_TOOL_RESULTS_INVALID",
                )
                for result in results:
                    if result.get("requestor") == "assistant":
                        counts["agent_tool_results"] += 1
                    elif result.get("requestor") != "user":
                        counts["tool_results_without_requestor"] += 1
    return {
        "messages_available": messages is not None,
        **{
            name: counts[name] if messages is not None else None
            for name in (
                "assistant_messages",
                "emitted_agent_tool_calls",
                "agent_tool_results",
                "tool_results_without_requestor",
            )
        },
        "safe_model_attempts_recorded": counts["safe_model_attempts"]
        if system == "safe_agent" and counts["assistant_messages_with_attempt_usage"]
        else None,
        "safe_assistant_messages_without_attempt_usage": counts[
            "assistant_messages_without_attempt_usage"
        ]
        if system == "safe_agent" and messages is not None
        else None,
        "actual_provider_attempts": None,
        "basis": "recorded assistant responses, emitted call entries, attributed tool results, and optional SAFE usage; unrecorded retries unknown",
    }


def _trial_view(run: _SavedRun, pair: Pair) -> dict[str, Any]:
    row = run.rows.get(pair)
    return {
        "trial": pair[1],
        "outcome": _outcome(row),
        "recorded_reward": _reward(row) if row else None,
        "recorded_seed": row["seed"] if row else None,
        "simulation_sha256": content_hash(row) if row else None,
        "duration_seconds": row.get("duration") if row and row.get("termination_reason") != "infrastructure_error" else None,
        "duration_basis": "not_retained_in_infrastructure_placeholder" if row and row.get("termination_reason") == "infrastructure_error" else "upstream_recorded_duration",
        "execution_state": next(
            (item["state"] for item in run.summary["pairs"] if (item["task_id"], item["trial"]) == pair),
            "unrecorded_status_unknown",
        ),
        "costs": run.pair_costs[pair],
        "calls": _call_counts(row, run.config.system),
    }


def _task_view(run: _SavedRun, task: str) -> dict[str, Any]:
    trials = [_trial_view(run, (task, trial)) for trial in range(run.config.num_trials)]
    outcomes = [row["outcome"] for row in trials]
    durations = [row["duration_seconds"] for row in trials if row["duration_seconds"] is not None]
    known_cost = sum((Decimal(row["costs"]["known_total_usd"]) for row in trials), Decimal(0))
    cost_complete = all(row["costs"]["complete"] for row in trials)
    counts = [row["calls"] for row in trials]
    return {
        "outcomes": dict(Counter(outcomes)),
        "conservative_pass_hat_ks": {
            str(k): pass_hat_k(len(trials), outcomes.count("success"), k)
            for k in (1, 4)
            if len(trials) >= k
        },
        "mean_duration_seconds": sum(durations) / len(durations)
        if len(durations) == len(trials)
        else None,
        "recorded_duration_samples": len(durations),
        "known_attributed_cost_usd": str(known_cost),
        "total_cost_usd_if_complete": str(known_cost) if cost_complete else None,
        "cost_allocation_complete": cost_complete,
        "calls": {
            **{
                "recorded_" + name: sum(item[name] or 0 for item in counts)
                for name in (
                    "assistant_messages",
                    "emitted_agent_tool_calls",
                    "agent_tool_results",
                    "tool_results_without_requestor",
                )
            },
            "trials_missing_message_records": sum(
                not item["messages_available"] for item in counts
            ),
            "safe_model_attempts_recorded": sum(
                item["safe_model_attempts_recorded"] or 0 for item in counts
            )
            if any(item["safe_model_attempts_recorded"] is not None for item in counts)
            else None,
            "safe_assistant_messages_without_attempt_usage": sum(
                item["safe_assistant_messages_without_attempt_usage"] or 0 for item in counts
            )
            if run.config.system == "safe_agent"
            else None,
            "actual_provider_attempts": None,
            "basis": "sums of recorded observations; missing messages and unrecorded retries remain unknown",
        },
        "trials": trials,
    }


def compare_saved_runs(
    left_run_dir: Path,
    right_run_dir: Path,
    *,
    bootstrap_samples: int = 2000,
    seed: int = 300,
    failure_annotations: Path | None = None,
) -> dict[str, Any]:
    """Validate matched evidence and return aggregate/per-task data; write nothing."""
    try:
        return _compare(left_run_dir, right_run_dir, bootstrap_samples, seed, failure_annotations)
    except ComparisonError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ComparisonError("INVALID_OR_UNREADABLE_COMPARISON_INPUT") from None


def _compare(
    left_path: Path, right_path: Path, samples: int, seed: int, annotations: Path | None
) -> dict[str, Any]:
    left, right = _load(left_path), _load(right_path)
    _match(left, right)
    transport_controls = {
        side: _agent_transport_controls(run.config, manifest=run.manifest)
        for side, run in (("left", left), ("right", right))
    }
    tasks = left.manifest["inventory"]["selected_task_ids"]
    joined = [
        {"task_id": task, "left": _task_view(left, task), "right": _task_view(right, task)}
        for task in tasks
    ]
    performance = {
        str(k): paired_task_interval(
            [task["left"]["conservative_pass_hat_ks"][str(k)] for task in joined],
            [task["right"]["conservative_pass_hat_ks"][str(k)] for task in joined],
            samples=samples,
            seed=seed,
        )
        for k in (1, 4)
        if left.config.num_trials >= k
    }
    latency_pairs = [
        task
        for task in joined
        if all(task[side]["mean_duration_seconds"] is not None for side in ("left", "right"))
    ]
    cost_pairs = [
        task
        for task in joined
        if all(task[side]["cost_allocation_complete"] for side in ("left", "right"))
    ]
    return {
        "schema_version": 1,
        "kind": "matched_recorded_tau3_text_comparison",
        "tau_commit": TAU_COMMIT,
        "domain": left.config.domain,
        "stage": left.config.stage,
        "matched_metadata_valid": True,
        "agent_transport_comparability": _transport_comparability(transport_controls),
        "shared_condition": {
            "model_route": left.config.model_route,
            "local_api_base": left.config.local_api_base,
            "model_weights_or_hardware_attested": False,
            "model_identity_basis": "declared route, endpoint and model aliases; mutable local aliases are not weight attestations",
            "agent_model": left.config.llm_agent,
            "user_model": left.config.llm_user,
            "judge_model": left.manifest["judge_model"],
            "agent_args_sha256": content_hash(left.config.llm_args_agent),
            "user_args_sha256": content_hash(left.config.llm_args_user),
            "judge_args_sha256": content_hash(left.manifest["judge_args"]),
            "retrieval": left.config.retrieval_config,
            "retrieval_args_sha256": content_hash(left.config.retrieval_config_kwargs),
            "seed": left.config.seed,
            "num_trials": left.config.num_trials,
            "task_content_sha256": left.manifest["inventory"]["task_content_sha256"],
            "source_manifest_sha256": left.manifest["inventory"]["source_verification"][
                "manifest_sha256"
            ],
            "recorded_domain_policy_sha256": content_hash(left.info["environment_info"]["policy"])
            if left.info is not None
            else None,
            "recorded_user_guidelines_sha256": content_hash(
                left.info["user_info"]["global_simulation_guidelines"]
            )
            if left.info is not None
            else None,
        },
        "comparison_direction": "left minus right; positive success difference favors left",
        "left": {
            "run_id": left.config.run_id,
            "system": left.config.system,
            "provenance": left.provenance,
            "report": left.summary,
            "saved_official_metrics": left.official_metrics,
        },
        "right": {
            "run_id": right.config.run_id,
            "system": right.config.system,
            "provenance": right.provenance,
            "report": right.summary,
            "saved_official_metrics": right.official_metrics,
        },
        "system_specific_conditions": {
            side: {
                "episode_budget_sha256": content_hash(run.config.episode_budget),
                "run_budget_sha256": content_hash(run.config.run_budget),
                "agent_transport_controls": transport_controls[side],
            }
            for side, run in (("left", left), ("right", right))
        },
        "denominators": {
            "scheduled_tasks": len(tasks),
            "scheduled_pairs_per_system": len(left.scheduled),
            "conservative": "all scheduled pairs; missing, infrastructure and ungraded outcomes fail",
            "official": "saved upstream metrics retained separately; upstream excludes infrastructure and can reduce eligible trials/tasks",
            "official_difference_interval": None,
            "official_difference_interval_reason": "no interval is substituted for potentially different official eligible populations",
        },
        "paired_conservative_pass_hat_ks": performance,
        "paired_latency_seconds": paired_task_interval(
            [task["left"]["mean_duration_seconds"] for task in latency_pairs],
            [task["right"]["mean_duration_seconds"] for task in latency_pairs],
            samples=samples,
            seed=seed,
        ),
        "paired_cost_usd_per_scheduled_conversation": paired_task_interval(
            [
                float(Decimal(task["left"]["total_cost_usd_if_complete"]) / left.config.num_trials)
                for task in cost_pairs
            ],
            [
                float(
                    Decimal(task["right"]["total_cost_usd_if_complete"]) / right.config.num_trials
                )
                for task in cost_pairs
            ],
            samples=samples,
            seed=seed,
        ),
        "resource_comparison_missingness": {
            "latency_excluded_tasks": len(tasks) - len(latency_pairs),
            "cost_excluded_tasks": len(tasks) - len(cost_pairs),
            "basis": "only tasks with every scheduled trial observed/allocated in both systems; no zero imputation",
            "latency_scope": "recorded end-to-end simulation duration; not isolated model latency",
        },
        "interval_method": {
            "unit": "paired task; all trials stay together",
            "method": "95% percentile bootstrap of task-level differences, fixed Python Random seed",
            "conditional_scope": "task variability conditional on recorded runs; not all model/simulator randomness",
            "limitations": "task independence assumed; family correlation and finite-benchmark population corrections not modeled; small samples can be unstable",
        },
        "reviewed_failure_taxonomy": _failure_reviews(annotations, (left, right)),
        "per_task": joined,
        "fresh_judge_calls": 0,
        "writes_performed": 0,
    }
