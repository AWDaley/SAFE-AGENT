"""Prepare a private custom-submission bundle from complete saved evaluations.

This evaluator-side tool never runs a model, rejudges a conversation, or publishes
anything. Public upstream checks establish file/task/metric consistency, not an
independent attestation that a model actually produced the supplied records.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl

from . import TAU_COMMIT
from .config import BenchmarkConfig
from .inventory import content_hash, inspect_benchmark
from .protocol import BoundaryModel
from .reporting import summarize_saved_results
from .runner import _write_json, prepare_official_import


class SubmissionError(ValueError):
    """A saved run cannot support the requested submission bundle."""


class SubmissionMetadata(BoundaryModel):
    """Submitter-provided facts; organization and contact details are not guessed."""

    model_name: str = Field(min_length=1, max_length=200)
    model_organization: str = Field(min_length=1, max_length=200)
    submitting_organization: str = Field(min_length=1, max_length=200)
    submission_date: date
    contact_info: dict[str, str | None]
    architecture_notes: str = Field(min_length=20, max_length=20_000)
    development_exposure_notes: str = Field(min_length=20, max_length=20_000)
    implementation_url: HttpUrl


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SubmissionError(code)


def _read_file(path: Path, *, max_bytes: int) -> bytes:
    _require(path.is_file() and not path.is_symlink(), "REQUIRED_REGULAR_FILE_MISSING")
    _require(path.stat().st_size <= max_bytes, "SUBMISSION_INPUT_TOO_LARGE")
    return path.read_bytes()


def _load_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            _require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise SubmissionError("NONFINITE_JSON_NUMBER")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def validate_run_manifest(
    manifest: dict[str, Any], inventory: dict[str, Any]
) -> BenchmarkConfig:
    """Require the declared final condition and a freshly verified full inventory."""
    _require(
        manifest.get("kind") == "official_tau3_text"
        and manifest.get("tau_commit") == TAU_COMMIT
        and manifest.get("system") == "safe_agent"
        and manifest.get("runtime_mode") == "canonical_safe"
        and manifest.get("status") == "complete",
        "COMPLETE_CANONICAL_SAFE_RUN_REQUIRED",
    )
    config = BenchmarkConfig.model_validate(manifest["config"])
    _require(
        config.stage == "final"
        and config.system == "safe_agent"
        and config.run_id == manifest.get("run_id")
        and manifest.get("config_sha256") == content_hash(config.model_dump())
        and manifest.get("evaluation_type") == "ALL"
        and manifest.get("automatic_review") is False
        and manifest.get("retries") == 0,
        "FROZEN_FINAL_CONFIGURATION_REQUIRED",
    )
    revision = manifest.get("safe_revision") or {}
    _require(
        revision.get("dirty") is False
        and bool(re.fullmatch(r"[0-9a-f]{40}", str(revision.get("commit", ""))))
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(revision.get("runtime_sources_sha256", "")))),
        "CLEAN_RECORDED_SOURCE_REVISION_REQUIRED",
    )
    original = manifest.get("inventory") or {}
    _require(
        inventory.get("status") == "ready"
        and inventory.get("source_verified") is True
        and inventory.get("selected_task_count") == inventory.get("domain_task_count")
        and original.get("source_verified") is True,
        "COMPLETE_VERIFIED_DOMAIN_REQUIRED",
    )
    for key in (
        "domain", "domain_task_count", "selected_task_count", "selected_task_ids",
        "task_ids_sha256", "task_content_sha256", "selected_grading_basis_counts",
        "selected_tasks_with_nl_assertions",
    ):
        _require(key in inventory and inventory[key] == original.get(key), "INVENTORY_CHANGED")
    _require(
        inventory["source_verification"]["manifest_sha256"]
        == original.get("source_verification", {}).get("manifest_sha256"),
        "SOURCE_MANIFEST_CHANGED",
    )
    expected = [
        {"task_id": task_id, "trial": trial}
        for task_id in inventory["selected_task_ids"]
        for trial in range(config.num_trials)
    ]
    _require(manifest.get("scheduled") == expected, "FINAL_SCHEDULE_CHANGED")
    return config


def _validate_results_metadata(results: Any, config: BenchmarkConfig) -> None:
    info = results.info
    _require(len({task.id for task in results.tasks}) == len(results.tasks),
             "DUPLICATE_TASK_DEFINITION")
    _require(len({row.id for row in results.simulations}) == len(results.simulations)
             and all(row.id for row in results.simulations), "DUPLICATE_SIMULATION_ID")
    _require(
        info.environment_info.domain_name == config.domain
        and info.num_trials == config.num_trials
        and info.max_steps == config.max_steps
        and info.max_errors == config.max_errors
        and info.seed == config.seed
        and info.agent_info.implementation == "safe_agent_" + config.run_id
        and info.user_info.implementation == "user_simulator"
        and info.agent_info.llm == config.llm_agent
        and info.agent_info.llm_args == config.llm_args_agent
        and info.user_info.llm == config.llm_user
        and info.user_info.llm_args == config.llm_args_user,
        "TRAJECTORY_CONFIGURATION_MISMATCH",
    )
    if config.domain == "banking_knowledge":
        _require(
            info.retrieval_config == config.retrieval_config
            and (info.retrieval_config_kwargs or {}) == config.retrieval_config_kwargs,
            "TRAJECTORY_RETRIEVAL_MISMATCH",
        )
    _require(
        all(row.mode == "half_duplex" and row.messages is not None
            and (row.messages or row.termination_reason.value not in {"agent_stop", "user_stop"})
            for row in results.simulations),
        "COMPLETE_TEXT_TRAJECTORIES_REQUIRED",
    )
    tasks = {task.id: task for task in results.tasks}
    _require(all(row.task_id in tasks for row in results.simulations),
             "TRAJECTORY_TASK_DEFINITION_MISSING")
    for row in results.simulations:
        criteria = tasks[row.task_id].evaluation_criteria
        reward = row.reward_info
        if row.termination_reason.value not in {"agent_stop", "user_stop"}:
            # The pinned evaluator assigns zero to premature termination. Keep
            # these failed attempts rather than requiring successful dialogue.
            _require(reward.reward == 0 and reward.reward_basis is None,
                     "PREMATURE_TERMINATION_REWARD_CHANGED")
        elif criteria is not None:
            _require(reward.reward_basis == criteria.reward_basis,
                     "RECORDED_REWARD_BASIS_MISMATCH")
            breakdown = reward.reward_breakdown or {}
            _require(set(breakdown) == set(criteria.reward_basis)
                     and math.isclose(math.prod(breakdown.values()), reward.reward,
                                      rel_tol=0, abs_tol=1e-8),
                     "RECORDED_REWARD_BREAKDOWN_MISMATCH")
            if any(basis.value == "NL_ASSERTION" for basis in criteria.reward_basis):
                _require([check.nl_assertion for check in reward.nl_assertions or []]
                         == (criteria.nl_assertions or []),
                         "RECORDED_JUDGE_OUTPUTS_MISSING")
                _require(breakdown["NL_ASSERTION"]
                         == float(all(check.met for check in reward.nl_assertions or [])),
                         "RECORDED_JUDGE_REWARD_MISMATCH")


def _payload(
    metadata: SubmissionMetadata, manifest: dict[str, Any], metrics: dict[str, Any]
) -> dict[str, Any]:
    config = manifest["config"]
    domain = config["domain"]
    values = metrics["pass_hat_ks"]
    scores = {f"pass_{k}": values.get(str(k), values.get(k)) for k in range(1, 5)}
    _require(
        all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
            for value in scores.values()),
        "ALL_FOUR_RECORDED_METRICS_REQUIRED",
    )
    domain_result = {key: 100 * value for key, value in scores.items()}
    if domain == "banking_knowledge":
        domain_result["retrieval_config"] = config["retrieval_config"]
    notes = "\n\n".join([
        metadata.architecture_notes,
        "Development exposure: " + metadata.development_exposure_notes,
        "Declared development task IDs: " + json.dumps(config["development_task_ids"]),
        "Frozen source: " + TAU_COMMIT + "; SAFE commit: "
        + manifest["safe_revision"]["commit"] + ". Agent model: " + config["llm_agent"],
        "Recorded EvaluationType.ALL rewards; no fresh judging. SAFE changes the agent "
        "scaffold and prompts. Encoded domain rules cover a documented mechanical policy "
        "subset. Reservations are not invoices; submission cost is omitted because this "
        "bundle does not independently verify provider billing. Full saved trajectories "
        "are included for private review. Publication remains a separate decision.",
    ])
    return {
        "model_name": metadata.model_name,
        "model_organization": metadata.model_organization,
        "submitting_organization": metadata.submitting_organization,
        "submission_date": metadata.submission_date.isoformat(),
        "submission_type": "custom", "modality": "text",
        "contact_info": metadata.contact_info,
        "trajectories_available": True,
        "trajectory_files": {domain: domain + ".json"},
        "results": {domain: domain_result},
        "references": [{"title": "SAFE implementation", "url": str(metadata.implementation_url),
                        "type": "github"}],
        "methodology": {
            "tau2_bench_version": "1.0.1", "user_simulator": config["llm_user"],
            "notes": notes,
            "verification": {
                "modified_prompts": True, "omitted_questions": False,
                "details": "Complete saved matrix; pinned public task/trial/schema checks; "
                "metrics recomputed from saved rewards. This is consistency validation, "
                "not independent run authenticity or leaderboard acceptance.",
            },
        },
    }


def prepare_submission(
    run_dir: Path, metadata_path: Path, output_dir: Path, *, tau_root: Path | None = None
) -> dict[str, Any]:
    """Validate and create a new private bundle; never mutate or replace run input."""
    run_dir = run_dir.resolve(strict=True)
    output_dir = output_dir.absolute()
    _require(not output_dir.exists(), "SUBMISSION_OUTPUT_MUST_NOT_EXIST")
    _require(not output_dir.resolve().is_relative_to(run_dir), "OUTPUT_MUST_BE_OUTSIDE_RUN")
    manifest_raw = _read_file(run_dir / "manifest.json", max_bytes=4_000_000)
    results_raw = _read_file(run_dir / "results.json", max_bytes=256_000_000)
    official_raw = _read_file(run_dir / "official_metrics.json", max_bytes=4_000_000)
    manifest = _load_json(manifest_raw)
    _require(manifest.get("saved_artifacts_sha256") == {
        "results.json": hashlib.sha256(results_raw).hexdigest(),
        "official_metrics.json": hashlib.sha256(official_raw).hexdigest(),
    }, "SAVED_ARTIFACT_HASH_MISMATCH")
    config = BenchmarkConfig.model_validate(manifest["config"])
    root = tau_root or Path(config.tau_root or manifest.get("tau_root", ""))
    inventory = inspect_benchmark(config, root)
    config = validate_run_manifest(manifest, inventory)
    metadata = SubmissionMetadata.model_validate_json(
        _read_file(metadata_path, max_bytes=100_000)
    )
    raw_results = _load_json(results_raw)
    report = summarize_saved_results(manifest, raw_results.get("simulations", []))
    _require(
        report["coverage"]["matrix_complete"]
        and report["coverage"]["infrastructure_failures"] == 0
        and report["coverage"]["ungraded"] == 0
        and report["coverage"]["terminal"] == len(manifest["scheduled"]),
        "COMPLETE_GRADED_TRAJECTORY_MATRIX_REQUIRED",
    )
    prepare_official_import(root)
    from tau2.config import DEFAULT_LLM_NL_ASSERTIONS, DEFAULT_LLM_NL_ASSERTIONS_ARGS
    from tau2.data_model.simulation import Results
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.scripts.leaderboard.submission import Submission
    from tau2.scripts.leaderboard.verify_trajectories_public import check_num_trials, check_tasks

    _require(manifest.get("judge_model") == DEFAULT_LLM_NL_ASSERTIONS
             and manifest.get("judge_args") == DEFAULT_LLM_NL_ASSERTIONS_ARGS,
             "PINNED_JUDGE_CONFIGURATION_REQUIRED")
    results = Results.model_validate(raw_results)
    _validate_results_metadata(results, config)
    for check in (check_tasks, check_num_trials):
        passed, _ = check(results)
        _require(passed, "PINNED_PUBLIC_TRAJECTORY_CHECK_FAILED")
    metrics = compute_metrics(results).model_dump(mode="json")
    _require(metrics == _load_json(official_raw), "SAVED_OFFICIAL_METRICS_MISMATCH")
    payload = Submission.model_validate(_payload(metadata, manifest, metrics))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tau3-submission-", dir=output_dir.parent))
    try:
        (temporary / "trajectories").mkdir(mode=0o700)
        trajectory = temporary / "trajectories" / (config.domain + ".json")
        trajectory.write_bytes(results_raw)
        trajectory.chmod(0o600)
        _write_json(temporary / "submission.json", payload.model_dump(mode="json", exclude_none=True))
        _write_json(temporary / "official_metrics.json", metrics)
        _write_json(temporary / "disclosures.json", {
            "run_id": config.run_id, "tau_commit": TAU_COMMIT,
            "config_sha256": manifest["config_sha256"],
            "safe_revision": manifest["safe_revision"],
            "config": {key: value for key, value in config.model_dump().items()
                       if key not in {"output_dir", "tau_root"}},
            "inventory": inventory, "judge_model": manifest.get("judge_model"),
            "judge_args": manifest.get("judge_args"),
            "dependencies": manifest.get("dependencies"),
            "input_sha256": {
                "manifest.json": hashlib.sha256(manifest_raw).hexdigest(),
                "results.json": hashlib.sha256(results_raw).hexdigest(),
                "official_metrics.json": hashlib.sha256(official_raw).hexdigest(),
            },
        })
        validation = {
            "kind": "private_custom_submission_bundle", "status": "validated_saved_records",
            "tau_commit": TAU_COMMIT, "run_id": config.run_id,
            "scheduled_conversations": len(manifest["scheduled"]),
            "checks": ["full_manifest_matrix", "pinned_source_inventory", "public_task_equality",
                       "public_trial_counts", "upstream_submission_schema", "saved_reward_metrics"],
            "fresh_model_calls": 0, "fresh_judging": False, "published": False,
            "independent_run_authenticity_verified": False,
            "files_sha256": {
                str(path.relative_to(temporary)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(temporary.rglob("*")) if path.is_file()
            },
        }
        _write_json(temporary / "validation.json", validation)
        # mkdir and link both fail if the destination already exists. A plain
        # rename could replace an empty directory created after the first check.
        output_dir.mkdir(mode=0o700)
        created: list[Path] = [output_dir]
        try:
            (output_dir / "trajectories").mkdir(mode=0o700)
            created.append(output_dir / "trajectories")
            sources = sorted(path for path in temporary.rglob("*") if path.is_file()
                             and path.name != "validation.json")
            sources.append(temporary / "validation.json")  # completion record comes last
            for source in sources:
                target = output_dir / source.relative_to(temporary)
                os.link(source, target)
                created.append(target)
        except BaseException:
            for path in reversed(created):
                try:
                    path.rmdir() if path.is_dir() else path.unlink()
                except OSError:
                    pass
            raise
        shutil.rmtree(temporary)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return validation
