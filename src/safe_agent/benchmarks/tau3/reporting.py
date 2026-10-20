"""Coverage and cost reporting from recorded rewards; never re-run a judge."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import TAU_COMMIT
from .runner import _write_json


def pass_hat_k(trials: int, successes: int, k: int) -> float:
    """Pinned estimator: probability all k draws without replacement succeed."""
    if not 0 <= successes <= trials or not 1 <= k <= trials:
        raise ValueError("Require 0 <= successes <= trials and 1 <= k <= trials")
    return math.comb(successes, k) / math.comb(trials, k)


def _success(reward: Any) -> bool:
    return (
        isinstance(reward, (float, int))
        and not isinstance(reward, bool)
        and abs(reward - 1) <= 1e-6
    )


def _reward(simulation: dict[str, Any]) -> Any:
    return (simulation.get("reward_info") or {}).get("reward")


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid recorded cost") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Recorded costs must be finite and nonnegative")
    return result


def summarize_saved_results(
    manifest: dict[str, Any],
    simulations: list[dict[str, Any]],
    additional_costs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if manifest.get("tau_commit") != TAU_COMMIT:
        raise ValueError("Results belong to a different benchmark revision")
    if manifest.get("kind") != "official_tau3_text":
        raise ValueError("Scripted fixtures cannot be reported as official benchmark runs")
    scheduled = [(str(row["task_id"]), row["trial"]) for row in manifest["scheduled"]]
    if len(scheduled) != len(set(scheduled)):
        raise ValueError("Manifest contains duplicate scheduled task/trial pairs")
    expected = set(scheduled)
    by_pair: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
    for simulation in simulations:
        by_pair[(str(simulation["task_id"]), simulation.get("trial"))].append(simulation)
    duplicate_pairs = [pair for pair, rows in by_pair.items() if len(rows) > 1]
    unexpected = sorted(set(by_pair) - expected, key=str)
    missing = sorted(expected - set(by_pair), key=str)
    infra = [row for row in simulations if row.get("termination_reason") == "infrastructure_error"]
    ungraded = [row for row in simulations if _reward(row) is None]
    graded = [row for row in simulations if _reward(row) is not None]
    eligible = [
        row for row in simulations if row.get("termination_reason") != "infrastructure_error"
    ]
    termination_counts = Counter(row.get("termination_reason", "missing") for row in simulations)
    safe_inference: Counter[str] = Counter()
    usage_sources: Counter[str] = Counter()
    for row in simulations:
        for message in row.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            for key in (
                "safe_model_attempts",
                "safe_reserved_cost_microusd",
                "input_tokens",
                "output_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    safe_inference[key] += value
            for source in usage.get("safe_usage_source") or []:
                usage_sources[str(source)] += 1
    task_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        task_groups[str(row["task_id"])].append(row)
    valid_metric_input = (
        not duplicate_pairs and not unexpected and not any(_reward(row) is None for row in eligible)
    )
    metric_values: dict[str, float] = {}
    if valid_metric_input and task_groups:
        max_k = min(len(rows) for rows in task_groups.values())
        max_k = min(max_k, manifest["config"]["num_trials"])
        for k in range(1, max_k + 1):
            metric_values[str(k)] = sum(
                pass_hat_k(len(rows), sum(_success(_reward(row)) for row in rows), k)
                for rows in task_groups.values()
            ) / len(task_groups)

    operational: dict[str, list[bool]] = defaultdict(list)
    for pair in scheduled:
        rows = by_pair.get(pair, [])
        operational[pair[0]].append(
            len(rows) == 1
            and rows[0].get("termination_reason") != "infrastructure_error"
            and _success(_reward(rows[0]))
        )
    operational_metrics = {}
    if operational:
        for k in (1, 4):
            if all(len(values) >= k for values in operational.values()):
                operational_metrics[str(k)] = sum(
                    pass_hat_k(len(values), sum(values), k) for values in operational.values()
                ) / len(operational)

    known_costs: dict[str, Decimal] = defaultdict(Decimal)
    missing_costs: Counter[str] = Counter()
    for row in simulations:
        for category in ("agent", "user"):
            amount = _money(row.get(category + "_cost"))
            if amount is None:
                missing_costs[category] += 1
            else:
                known_costs[category] += amount
    additional = additional_costs or []
    categories_seen = {"agent", "user"} if simulations else set()
    for event in additional:
        category = event["category"]
        if category in {"agent", "user"}:
            raise ValueError(
                "Additional costs must not double-count upstream agent/user aggregates"
            )
        if category not in {
            "judge",
            "review",
            "retrieval",
            "infrastructure",
            "agent_extra",
            "failed_request",
        }:
            raise ValueError("Unknown additional cost category")
        categories_seen.add(category)
        amount = _money(event.get("amount_usd"))
        if amount is None:
            missing_costs[category] += 1
        else:
            known_costs[category] += amount
    unavailable_categories = sorted(
        set(manifest.get("expected_cost_components", [])) - categories_seen
    )
    successful_attempts = sum(_success(_reward(row)) for row in simulations)
    operating_cost = known_costs["agent"] + known_costs["agent_extra"] + known_costs["retrieval"]
    cost_complete = not missing_costs and not unavailable_categories and not missing
    costs = {
        "known_usd_by_category": {key: str(value) for key, value in sorted(known_costs.items())},
        "known_total_usd": str(sum(known_costs.values(), Decimal(0))),
        "missing_cost_entries": dict(missing_costs),
        "unreported_components": unavailable_categories,
        "complete": cost_complete,
        "known_agent_operating_cost_per_success_usd": (
            str(operating_cost / successful_attempts) if successful_attempts else None
        ),
        "cost_per_success_is_lower_bound": not cost_complete,
    }
    return {
        "run_id": manifest["run_id"],
        "system": manifest["system"],
        "tau_commit": TAU_COMMIT,
        "report_basis": "recorded rewards only; no fresh judging",
        "coverage": {
            "scheduled": len(scheduled),
            "recorded_attempts": len(simulations),
            "attempted_pairs": len(set(by_pair) & expected),
            "terminal": sum(bool(row.get("termination_reason")) for row in simulations),
            "graded": len(graded),
            "ungraded": len(ungraded),
            "infrastructure_failures": len(infra),
            "missing_pairs": missing,
            "unexpected_pairs": unexpected,
            "duplicate_pairs": duplicate_pairs,
            "matrix_complete": not (missing or unexpected or duplicate_pairs),
        },
        "saved_reward_metrics": {
            "method": "mirror of pinned pass_hat_k; authoritative metrics remain upstream",
            "input_valid": valid_metric_input,
            "eligible_attempts": len(eligible),
            "eligible_tasks": len(task_groups),
            "pass_hat_ks": metric_values,
            "avg_reward": sum(_reward(row) for row in eligible) / len(eligible)
            if valid_metric_input and eligible
            else None,
            "infra_errors_excluded": True,
        },
        "conservative_operational_metrics": {
            "method": "all scheduled pairs; missing/ambiguous/infra outcomes count as failures",
            "pass_hat_ks": operational_metrics,
        },
        "termination_counts": dict(termination_counts),
        "safe_inference_recorded_usage": {
            **dict(safe_inference),
            "sources": dict(usage_sources),
            "reserved_cost_is_actual_spend": False,
            "billing_verified": False,
        },
        "costs": costs,
    }


def report_saved_run(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    results_path = run_dir / "results.json"
    results = (
        json.loads(results_path.read_text()) if results_path.is_file() else {"simulations": []}
    )
    costs_path = run_dir / "additional_costs.jsonl"
    costs = (
        [json.loads(line) for line in costs_path.read_text().splitlines() if line.strip()]
        if costs_path.exists()
        else []
    )
    report = summarize_saved_results(manifest, results.get("simulations", []), costs)
    official_path = run_dir / "official_metrics.json"
    report["official_metrics"] = (
        json.loads(official_path.read_text()) if official_path.exists() else None
    )
    _write_json(run_dir / "report.json", report)
    return report
