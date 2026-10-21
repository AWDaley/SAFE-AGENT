"""Read-only reservation calibration from a recorded canonical development pilot.

The multiplier expands the largest *observed* complete episode, not a statistical
tail bound. A suggestion is never a price estimate, a spend authorization, or a
claim that the target workload will finish. Scripted and censored evidence cannot
support suggestions. Recorded consistency is checked; origin authenticity is not.
"""

from __future__ import annotations

import importlib.metadata
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    strict_json,
    wire_digest,
)
from safe_agent.contracts.episodes import (
    BudgetDelta,
    EpisodeLimits,
    EpisodeState,
    EpisodeView,
    InferenceReservation,
    RunSpec,
)
from safe_agent.conversation.context_evidence import CONTEXT_RETENTION_PROFILE
from safe_agent.orchestration.episode_runtime import EpisodeRuntime, RunLimits, RuntimeLimits

from .comparison import ComparisonError, _load
from .config import CANONICAL_RUNTIME_FACTORY, BenchmarkConfig
from .diagnostics import (
    _context_diagnostics,
    _events_and_view,
    _inference_diagnostics,
    _report,
    _rows,
    _tool_diagnostics,
    _Totals,
)
from .inventory import content_hash, inspect_benchmark
from .reporting import _reward, _success
from .runner import _repository_revision

_UNITS = ("turns", "model_calls", "tool_calls", "tokens")
_DEPENDENCIES = ("tervoro-safe-agent", "tau2", "pydantic", "litellm", "rank-bm25")
_MATCHED_CONDITIONS = (
    "domain", "llm_agent", "llm_args_agent", "llm_user", "llm_args_user", "seed",
    "max_steps", "max_errors", "timeout", "retrieval_config", "retrieval_config_kwargs",
)
_UNCHANGED_LIMITS = ("deadline_seconds", "max_prompt_bytes", "max_output_tokens", "max_schema_repairs")
_TERMINAL = frozenset({EpisodeState.FINISHED, EpisodeState.FAILED, EpisodeState.HANDOFF})


class CalibrationError(ValueError):
    """Fixed failure codes only; no private database, model, or exception text."""


def _require(condition: bool, code: str = "PILOT_LEDGER_BINDING_INVALID") -> None:
    if not condition:
        raise CalibrationError(code)


def _distribution(values: list[int]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "samples": len(ordered), "sum": sum(ordered),
        "minimum": ordered[0] if ordered else None,
        "median": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "maximum": ordered[-1] if ordered else None,
    }


def _base(config: BenchmarkConfig, factor: float) -> dict[str, Any]:
    return {
        "schema_version": "safe_agent.tau3_pilot_calibration.v1",
        "kind": "pilot_reservation_calibration", "status": "unavailable", "reasons": [],
        "target_config_sha256": content_hash(config.model_dump()),
        "headroom_factor": factor, "suggested_budget_patch": None,
        "derived_requirements": None, "provenance": None, "pilot": None,
        "target_scheduled_conversations": None, "per_episode_usage": None,
        "runtime_diagnostics": None,
        "method": "ceil(maximum complete observed episode units * multiplier), then multiply by target schedule",
        "quantile_method": "nearest rank, descriptive only",
        "cost_basis": "caller configured fixed per-attempt reservation hold, never provider prices",
        "invoice_estimate": False, "origin_authenticity_verified": False,
        "statistical_tail_guarantee": False, "target_completion_guaranteed": False,
        "review_required": True, "spend_authorized": False,
        "model_calls": 0, "writes_performed": 0,
        "limitations": [
            "A small selected pilot does not establish the target tasks' resource distribution.",
            "Known failed tasks and rejected proposals remain included; no success-only selection.",
            "Token reservations use runtime held units, not provider-reported token counts.",
            "SAFE reservations do not cap stock, simulator, judge, or infrastructure invoices.",
            "No deadline, prompt, output, repair, or per-attempt hold changes are suggested.",
        ],
    }


def _current_dependencies() -> dict[str, str]:
    return {name: importlib.metadata.version(name) for name in _DEPENDENCIES}


def _ledger(
    database: Path, config: BenchmarkConfig, limits: RuntimeLimits, run_limits: RunLimits,
    policy: str | None, trial_seeds: dict[str, int],
) -> tuple[list[EpisodeView], dict[str, Any], str]:
    """Reuse diagnostics' replay/check helpers inside one read-only SQLite snapshot."""
    path = database.resolve(strict=True)
    _require(path.is_file(), "PILOT_LEDGER_UNREADABLE")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    totals = _Totals()
    views: list[EpisodeView] = []
    contexts: list[str] = []
    run_id = config.run_id if config.run_id[0].isalpha() else "run_" + config.run_id
    try:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        runs = list(db.execute("SELECT * FROM safe_episode_runs"))
        _require(len(runs) == 1 and runs[0]["run_id"] == run_id)
        row = runs[0]
        spec = RunSpec.model_validate_json(row["spec_json"])
        expected = wire_digest({
            "profile": "tau3-v1.0.1-episode-v1",
            "context_retention_profile": CONTEXT_RETENTION_PROFILE,
            "domain": config.domain, "model": config.llm_agent, "model_args": config.llm_args_agent,
            "episode_limits": limits.model_dump(), "run_limits": run_limits.model_dump(),
        })
        _require(spec.run_id == run_id and sha256(spec) == row["spec_hash"]
                 and spec.policy_hash == expected and spec.limits == run_limits.episode_limits())
        usage = BudgetDelta(**{name: row[name] for name in BudgetDelta.model_fields})
        _require(row["state_version"] == usage.turns + usage.model_calls + usage.tool_calls)
        records = list(db.execute("SELECT * FROM safe_episodes ORDER BY episode_id LIMIT 16385"))
        _require(len(records) <= 16384, "PILOT_TOO_LARGE")
        totals.counts["episodes_recorded"] = len(records)
        for record in records:
            view, events = _events_and_view(
                record, _rows(db, "safe_episode_events", record["episode_id"])
            )
            _require(view.run_id == run_id and view.domain == config.domain
                     and view.limits == limits.episode_limits()
                     and spec.created_at <= view.created_at < view.deadline <= spec.deadline)
            totals.counts["episodes_verified"] += 1
            totals.states[view.state.value] += 1
            _tool_diagnostics(db, view, events, totals)
            _inference_diagnostics(db, view, events, totals)
            _context_diagnostics(db, view.episode_id, totals)
            reservations = {
                row["request_id"]: InferenceReservation.model_validate_json(row["record_json"])
                for row in _rows(db, "safe_episode_inference_reservations", view.episode_id)
            }
            for attempt in _rows(db, "safe_episode_model_attempts", view.episode_id):
                request = EpisodeInferenceRequest.model_validate_json(attempt["request_json"])
                # The official per-trial seed overrides the configured seed in
                # EpisodeRuntime.set_seed. An unjoined episode stays unavailable.
                _require(request.model == config.llm_agent
                         and request.temperature == config.llm_args_agent.get("temperature", 0.0)
                         and request.max_output_tokens == limits.max_output_tokens
                         and (view.episode_id not in trial_seeds
                              or request.seed == trial_seeds[view.episode_id]),
                         "PILOT_REQUEST_SETTINGS_MISMATCH")
                # Exact canonical _infer allowance, including rejected/unknown
                # attempts; provider token usage never replaces held units.
                held_tokens = len(request.model_dump_json().encode()) + 8192 + request.max_output_tokens
                _require(reservations[request.request_id].budget == BudgetDelta(
                    model_calls=1, tokens=held_tokens, cost_microusd=limits.max_attempt_cost_microusd,
                ), "PILOT_RESERVATION_REQUEST_MISMATCH")
                if attempt["reply_json"] is not None:
                    reply = EpisodeInferenceReply.model_validate_json(attempt["reply_json"])
                    _require(reply.output_tokens <= request.max_output_tokens
                             and reply.input_tokens + reply.output_tokens <= held_tokens,
                             "PILOT_REPLY_USAGE_INVALID")
            context_rows = _rows(db, "safe_episode_private_contexts", view.episode_id)
            if context_rows:
                context = strict_json(context_rows[0]["context_json"], max_bytes=32 * 1024 * 1024)
                _require(isinstance(context, dict))
                assert isinstance(context, dict)
                _require(context.get("domain") == view.domain
                         and context.get("policy_hash") == view.policy_hash
                         and (policy is None or context.get("policy") == policy)
                         and EpisodeRuntime._policy_hash(
                             context["domain"], context["policy"], context["tools"]
                         ) == view.policy_hash)
                contexts.append(context_rows[0]["context_hash"])
            _require(view.usage.cost_microusd == view.usage.model_calls * limits.max_attempt_cost_microusd)
            views.append(view)
        for name in BudgetDelta.model_fields:
            _require(getattr(usage, name) == sum(getattr(view.usage, name) for view in views)
                     and getattr(usage, name) <= getattr(spec.limits, "max_" + name))
        for table in (
            "safe_episode_events", "safe_episode_inference_reservations", "safe_episode_inference_results",
            "safe_episode_outgoing_calls", "safe_episode_tool_results", "safe_episode_private_contexts",
            "safe_episode_model_attempts",
        ):
            count = db.execute(
                f"SELECT count(*) FROM {table} AS r LEFT JOIN safe_episodes AS e "
                "ON r.episode_id=e.episode_id WHERE e.episode_id IS NULL"
            ).fetchone()[0]
            _require(count == 0)
        report = _report(totals, None, False)
        _require(report["integrity"]["passed"], "PILOT_LEDGER_INTEGRITY_FAILED")
        _require(usage.model_calls == report["model_attempts"]["reserved"])
        digest = content_hash({"run_spec": sha256(spec), "usage": usage.model_dump(),
                               "snapshots": [sha256(view) for view in views], "contexts": contexts})
        return views, report, digest
    finally:
        db.rollback()
        db.close()


def _result_episode_ids(rows: dict[Any, dict[str, Any]]) -> tuple[dict[str, int], int]:
    found: dict[str, int] = {}
    missing = 0
    for row in rows.values():
        ids = set()
        for message in row.get("messages") or []:
            if message.get("role") == "assistant":
                raw = message.get("raw_data") or {}
                value = raw.get("safe_episode_id")
                if value is not None:
                    _require(isinstance(value, str), "PILOT_RESULT_EPISODE_BINDING_INVALID")
                    ids.add(value)
        _require(len(ids) <= 1 and not (ids & found.keys()), "PILOT_RESULT_EPISODE_BINDING_INVALID")
        if not ids:
            missing += 1
        found.update({episode_id: row["seed"] for episode_id in ids})
    return found, missing


def calibrate_pilot(
    run_dir: Path, target_config: BenchmarkConfig, *, headroom_factor: float = 1.5,
) -> dict[str, Any]:
    """Suggest only needed cap increases; never infer, execute, or edit configuration.

    Missing/censored evidence returns ``status=unavailable`` with fixed reasons.
    Malformed or inconsistent evidence raises ``CalibrationError``. Successful
    suggestions require review and are conditional on the unchanged runtime and
    model condition. This cannot attest that an externally supplied run is real.
    """
    if (type(headroom_factor) not in {float, int} or not math.isfinite(headroom_factor)
            or not 1 <= headroom_factor <= 10):
        raise CalibrationError("CALIBRATION_HEADROOM_FACTOR_INVALID")
    try:
        return _calibrate(run_dir, target_config, float(headroom_factor))
    except CalibrationError:
        raise
    except ComparisonError:
        raise CalibrationError("PILOT_EVIDENCE_INVALID") from None
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise CalibrationError("CALIBRATION_INPUT_INVALID") from None
    except (OSError, sqlite3.Error):
        raise CalibrationError("PILOT_INPUT_UNREADABLE") from None


def _calibrate(run_dir: Path, target: BenchmarkConfig, factor: float) -> dict[str, Any]:
    target = BenchmarkConfig.model_validate(target.model_dump())
    _require(target.system == "safe_agent" and target.runtime_factory == CANONICAL_RUNTIME_FACTORY,
             "CALIBRATION_CANONICAL_SAFE_TARGET_REQUIRED")
    target_limits = RuntimeLimits.model_validate(target.episode_budget)
    target_run_limits = RunLimits.model_validate(target.run_budget)
    target_limits.episode_limits()
    target_run_limits.episode_limits()
    _require(all(getattr(target_limits, name) <= getattr(target_run_limits, name)
                 for name in EpisodeLimits.model_fields),
             "CALIBRATION_TARGET_CAP_CONTAINMENT_INVALID")
    report = _base(target, factor)
    reasons: list[str] = report["reasons"]
    if not (run_dir / "manifest.json").is_file():
        reasons.append("PILOT_MANIFEST_MISSING")
        return report
    saved = _load(run_dir)
    pilot = saved.config
    _require(pilot.system == "safe_agent" and pilot.stage == "development"
             and pilot.runtime_factory == CANONICAL_RUNTIME_FACTORY,
             "PILOT_CANONICAL_DEVELOPMENT_REQUIRED")
    limits = RuntimeLimits.model_validate(pilot.episode_budget)
    run_limits = RunLimits.model_validate(pilot.run_budget)
    temperature = pilot.llm_args_agent.get("temperature", 0.0)
    seed = pilot.llm_args_agent.get("seed")
    _require(not (set(pilot.llm_args_agent) - {"temperature", "seed"})
             and isinstance(temperature, (int, float)) and not isinstance(temperature, bool)
             and 0 <= temperature <= 2
             and (seed is None or type(seed) is int), "PILOT_MODEL_ARGUMENTS_INVALID")
    report["provenance"] = dict(saved.provenance)
    report["provenance"]["runtime_sources_sha256"] = saved.manifest["safe_revision"]["runtime_sources_sha256"]
    if saved.manifest.get("status") != "complete":
        reasons.append("PILOT_RUN_INCOMPLETE")
    if not saved.provenance["saved_artifact_binding_validated"]:
        reasons.append("PILOT_SAVED_ARTIFACT_BINDING_MISSING")
    # These are the unchanged NL-assertion defaults at the pinned v1.0.1 source.
    _require(saved.manifest["judge_model"] == "gpt-4.1-2025-04-14"
             and saved.manifest["judge_args"] == {"temperature": 0.0},
             "PILOT_PINNED_JUDGE_REQUIRED")
    if any(getattr(pilot, name) != getattr(target, name) for name in _MATCHED_CONDITIONS) or any(
        getattr(limits, name) != getattr(target_limits, name) for name in _UNCHANGED_LIMITS
    ):
        reasons.append("PILOT_CONDITION_MISMATCH")
    if _repository_revision()["runtime_sources_sha256"] != saved.manifest["safe_revision"]["runtime_sources_sha256"]:
        reasons.append("PILOT_RUNTIME_CHANGED")
    try:
        if saved.manifest["dependencies"] != _current_dependencies():
            reasons.append("PILOT_DEPENDENCIES_CHANGED")
    except importlib.metadata.PackageNotFoundError:
        reasons.append("PILOT_DEPENDENCIES_UNAVAILABLE")
    source = target.tau_root or pilot.tau_root or saved.manifest.get("tau_root")
    root = Path(source) if source else None
    inspection = inspect_benchmark(target, root)
    if inspection["status"] != "ready":
        reasons.append("TARGET_PINNED_SOURCES_UNAVAILABLE")
    else:
        report["target_scheduled_conversations"] = inspection["scheduled_conversations"]
        _require(0 < inspection["scheduled_conversations"] <= 16384,
                 "CALIBRATION_TARGET_SCHEDULE_TOO_LARGE")
        pilot_inspection = inspect_benchmark(pilot, root)
        _require(pilot_inspection["status"] == "ready" and all(
            pilot_inspection[name] == saved.manifest["inventory"][name]
            for name in ("selected_task_ids", "task_ids_sha256", "task_content_sha256")
        ), "PILOT_SOURCE_SCHEDULE_MISMATCH")
    terminations = Counter(row["termination_reason"] for row in saved.rows.values())
    report["pilot"] = {
        "scheduled_conversations": len(saved.scheduled), "recorded_conversations": len(saved.rows),
        "missing_conversations": len(saved.scheduled) - len(saved.rows),
        "graded_conversations": sum(_reward(row) is not None for row in saved.rows.values()),
        "successful_conversations": sum(_success(_reward(row)) for row in saved.rows.values()),
        "failed_graded_conversations": sum(_reward(row) is not None and not _success(_reward(row))
                                           for row in saved.rows.values()),
        "termination_counts": dict(sorted(terminations.items())),
        "development_task_ids_sha256": content_hash(saved.manifest["inventory"]["selected_task_ids"]),
        "recorded_actual_billing_complete": saved.summary["costs"]["complete"],
    }
    if set(saved.rows) != set(saved.scheduled):
        reasons.append("PILOT_RESULT_MATRIX_INCOMPLETE")
    if any(_reward(row) is None for row in saved.rows.values()):
        reasons.append("PILOT_HAS_UNGRADED_RESULTS")
    if set(terminations) - {"user_stop", "agent_stop"}:
        reasons.append("PILOT_HAS_CENSORED_TRAJECTORIES")
    database = run_dir / "private/episodes.sqlite"
    if not database.is_file():
        reasons.append("PILOT_LEDGER_MISSING")
        return report
    policy = saved.info["environment_info"]["policy"] if saved.info else None
    trial_seeds, missing_ids = _result_episode_ids(saved.rows)
    views, diagnostics, digest = _ledger(database, pilot, limits, run_limits, policy, trial_seeds)
    report["runtime_diagnostics"] = diagnostics
    report["provenance"]["ledger_snapshot_sha256"] = digest
    report["per_episode_usage"] = {
        name: _distribution([getattr(view.usage, name) for view in views])
        for name in (*_UNITS, "cost_microusd")
    }
    ids = set(trial_seeds)
    _require(ids <= {view.episode_id for view in views}, "PILOT_RESULT_EPISODE_BINDING_INVALID")
    if missing_ids or len(views) != len(saved.scheduled) or ids != {view.episode_id for view in views}:
        reasons.append("PILOT_LEDGER_RESULT_JOIN_INCOMPLETE")
    if (any(view.state not in _TERMINAL or view.pending_call_id or view.pending_request_id for view in views)
            or diagnostics["model_attempts"]["journal_unfinished"]
            or diagnostics["tool_calls"]["without_result"]
            or diagnostics["episodes"]["processing_claims_active"]):
        reasons.append("PILOT_HAS_UNRESOLVED_ATTEMPTS")
    if diagnostics["episodes"]["private_contexts_missing"]:
        reasons.append("PILOT_PRIVATE_CONTEXT_MISSING")
    attempts = diagnostics["model_attempts"]
    if (not attempts["reserved"] or attempts["known_replies"] != attempts["reserved"]
            or set(attempts["usage_sources"]) != {"provider_reported"}
            or any(view.usage.model_calls == 0 for view in views)):
        reasons.append("PILOT_COMPLETE_PROVIDER_USAGE_REQUIRED")
    if diagnostics["denials"]["by_reason"].get("DOMAIN_TOOL_BUDGET_EXHAUSTED"):
        reasons.append("PILOT_HAS_CENSORED_TRAJECTORIES")
    report["reasons"] = sorted(set(reasons))
    if report["reasons"]:
        return report
    count = report["target_scheduled_conversations"]
    episode = {"max_" + name: max(1, math.ceil(max(getattr(view.usage, name) for view in views) * factor))
               for name in _UNITS}
    episode["max_cost_microusd"] = episode["max_model_calls"] * target_limits.max_attempt_cost_microusd
    run = {name: value * count for name, value in episode.items()}
    # Validate derived requirements against the same finite runtime/storage bounds.
    RuntimeLimits.model_validate({**target_limits.model_dump(), **episode}).episode_limits()
    RunLimits.model_validate({**target_run_limits.model_dump(), **run}).episode_limits()
    report.update({
        "status": "review_required",
        "derived_requirements": {"episode_budget": episode, "run_budget": run,
                                 "unchanged_attempt_hold_microusd": target_limits.max_attempt_cost_microusd},
        "suggested_budget_patch": {
            "episode_budget": {name: value for name, value in episode.items() if value > getattr(target_limits, name)},
            "run_budget": {name: value for name, value in run.items() if value > getattr(target_run_limits, name)},
        },
    })
    return report
