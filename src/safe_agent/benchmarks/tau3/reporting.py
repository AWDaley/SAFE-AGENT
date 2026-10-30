"""Coverage and cost reporting from recorded rewards; never re-run a judge."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from safe_agent.contracts.episode_inference import validate_local_api_base

from . import TAU_COMMIT
from .execution import load_episode_index, public_execution, validate_episode_index
from .protocol import BoundaryModel
from .runner import _write_json

_USD_PATTERN = r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$"


class BillingAllocation(BoundaryModel):
    task_id: str = Field(min_length=1, max_length=200)
    trial: int = Field(ge=0)
    amount_usd: str = Field(pattern=_USD_PATTERN, max_length=64)


class AgentBillingReconciliation(BoundaryModel):
    """One reviewed full-run agent total, replacing its upstream aggregate.

    A reviewer/source hash supplies provenance, not provider attestation. The
    declared scope includes failed and repair attempts, never reservation holds.
    """

    category: Literal["agent_provider_billing"]
    run_id: str = Field(min_length=1, max_length=200)
    component: Literal["agent"]
    currency: Literal["USD"]
    scope: Literal["all_agent_attempts_including_failures_and_repairs"]
    replaces: Literal["upstream_agent_aggregate"]
    amount_usd: str = Field(pattern=_USD_PATTERN, max_length=64)
    event_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    allocations: list[BillingAllocation] | None = None


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


def _route_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    """Normalize only route metadata; old reports can contain partial configs."""
    config = manifest.get("config", {})
    route = config.get("model_route", "openai")
    base = config.get("local_api_base")
    if route == "local":
        try:
            if not isinstance(base, str):
                raise ValueError
            validate_local_api_base(base)
        except ValueError:
            raise ValueError("RECORDED_LOCAL_ROUTE_INVALID") from None
    elif route != "openai" or base is not None:
        raise ValueError("RECORDED_MODEL_ROUTE_INVALID")
    proposal_format = config.get("agent_proposal_format", "candidate_json_v1")
    if (proposal_format not in {"candidate_json_v1", "native_tools_v1", "native_tools_v2"}
            or (proposal_format != "candidate_json_v1" and (
                route != "local" or config.get("system") != "safe_agent"
            ))):
        raise ValueError("RECORDED_AGENT_PROPOSAL_FORMAT_INVALID")
    return {
        "model_route": route,
        "local_api_base": base,
        "agent_proposal_format": proposal_format,
        "defaulted_from_legacy_config": "model_route" not in config,
        "basis": "declared saved configuration; no independent endpoint, weight or hardware attestation",
        "model_weights_or_hardware_attested": False,
    }


def _launch_classification(manifest: dict[str, Any]) -> str:
    """Keep saved launch claims distinct from fresh evidence validation."""
    runtime_mode = manifest.get("runtime_mode")
    system = manifest.get("system")
    fixture_modes = {"injected_development", "custom_development"}
    if runtime_mode in fixture_modes or system in fixture_modes:
        return "development_fixture"
    declared = manifest.get("launch_classification")
    if declared is None:
        # Old optional gate metadata was accepted from a caller without reading
        # its files. Its presence cannot retroactively establish qualification.
        return "legacy_unqualified"
    if runtime_mode == "stock_baseline" and system == "llm_agent":
        return "stock_baseline"
    canonical_safe = runtime_mode == "canonical_safe" and system == "safe_agent"
    route = manifest.get("config", {}).get("model_route", "openai")
    if declared == "unqualified_hosted_live" and canonical_safe and route == "openai":
        return declared
    if declared == "qualified_local_live" and canonical_safe and route == "local":
        gate = manifest.get("qualification_gate")
        if (
            isinstance(gate, dict)
            and set(gate) == {"report_sha256", "review_sha256", "condition_sha256",
                              "review_is_independent_attestation"}
            and all(isinstance(gate.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{64}", gate[key]) is not None
                    for key in ("report_sha256", "review_sha256"))
            and isinstance(gate.get("condition_sha256"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", gate["condition_sha256"]) is not None
            and gate["review_is_independent_attestation"] is False
        ):
            return declared
    raise ValueError("RECORDED_LAUNCH_CLASSIFICATION_INVALID")


def cost_accounting(
    manifest: dict[str, Any],
    simulations: list[dict[str, Any]],
    additional_costs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[tuple[str, int | None], dict[str, Any]]]:
    """Recorded run totals and explicitly attributed pair costs, without inference.

    Missing per-pair allocations remain unknown even when a run invoice total is
    known. Exactly one full-run agent reconciliation can replace upstream agent
    charges; its provenance is asserted by the supplied reviewed ledger.
    """
    route = _route_provenance(manifest)["model_route"]
    expected_components = set(manifest.get("expected_cost_components", []))
    if route == "local":
        # LiteLLM can report zero when a local alias has no known token price.
        # It cannot establish the cost of running that model on local hardware.
        expected_components.add("infrastructure")
    additional = additional_costs or []
    reconciliation_rows = [
        event for event in additional if event.get("category") == "agent_provider_billing"
    ]
    if len(reconciliation_rows) > 1:
        raise ValueError("DUPLICATE_AGENT_BILLING_RECONCILIATION")
    reconciled: AgentBillingReconciliation | None = None
    if reconciliation_rows:
        try:
            reconciled = AgentBillingReconciliation.model_validate(reconciliation_rows[0])
        except ValueError:
            raise ValueError("AGENT_BILLING_RECONCILIATION_INVALID") from None
        if reconciled.run_id != manifest["run_id"]:
            raise ValueError("AGENT_BILLING_RUN_MISMATCH")
        if any(
            event.get("category") == "agent_extra"
            or (
                event.get("category") == "failed_request"
                and event.get("component") not in {"user", "judge", "retrieval", "infrastructure"}
            )
            for event in additional
        ):
            raise ValueError("AGENT_BILLING_WOULD_DOUBLE_COUNT_ADDITIONAL_CHARGES")
    scheduled = {(str(row["task_id"]), row["trial"]) for row in manifest["scheduled"]}
    grouped: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
    known: dict[str, Decimal] = defaultdict(Decimal)
    missing: Counter[str] = Counter()
    pair_known: dict[tuple[str, int | None], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    pair_seen: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    pair_missing: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    upstream_agent = Decimal(0)
    upstream_agent_missing = 0
    for row in simulations:
        pair = (str(row["task_id"]), row.get("trial"))
        grouped[pair].append(row)
        for category in ("agent", "user"):
            amount = _money(row.get(category + "_cost"))
            if category == "agent":
                if amount is None:
                    upstream_agent_missing += 1
                else:
                    upstream_agent += amount
                if reconciled is not None:
                    continue
            pair_seen[pair].add(category)
            if amount is None:
                missing[category] += 1
                pair_missing[pair].add(category)
            else:
                known[category] += amount
                pair_known[pair][category] += amount
    categories_seen = {"agent", "user"} if simulations else set()
    event_ids: set[str] = set()
    unallocated_components: set[str] = set()
    operating_failed_requests = Decimal(0)
    operating_attribution_unknown = False
    for event in additional:
        if "run_id" in event and event["run_id"] != manifest["run_id"]:
            raise ValueError("COST_EVENT_RUN_MISMATCH")
        if "event_id" in event:
            event_id = event["event_id"]
            if not isinstance(event_id, str) or not event_id or event_id in event_ids:
                raise ValueError("DUPLICATE_OR_INVALID_COST_EVENT_ID")
            event_ids.add(event_id)
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
            "agent_provider_billing",
        }:
            raise ValueError("Unknown additional cost category")
        categories_seen.add(category)
        amount = _money(event.get("amount_usd"))
        if amount is None:
            missing[category] += 1
        else:
            known[category] += amount
        if category == "agent_provider_billing":
            continue
        if category == "failed_request":
            component = event.get("component")
            if component is not None and component not in {
                "agent",
                "user",
                "judge",
                "retrieval",
                "infrastructure",
            }:
                raise ValueError("FAILED_REQUEST_COMPONENT_INVALID")
            if amount is not None and component in {"agent", "retrieval"}:
                operating_failed_requests += amount
            elif component is None and (amount is None or amount > 0):
                operating_attribution_unknown = True
        if ("task_id" in event) != ("trial" in event):
            raise ValueError("COST_ALLOCATION_REQUIRES_TASK_AND_TRIAL")
        if "task_id" in event:
            pair = (str(event["task_id"]), event["trial"])
            if type(pair[1]) is not int or pair not in scheduled:
                raise ValueError("COST_ALLOCATION_NOT_SCHEDULED")
            pair_seen[pair].add(category)
            if amount is None:
                pair_missing[pair].add(category)
            else:
                pair_known[pair][category] += amount
        elif amount is None or amount > 0:
            unallocated_components.add(category)
    if reconciled is not None:
        categories_seen.add("agent")
        if reconciled.allocations is not None:
            allocated: set[tuple[str, int | None]] = set()
            total = Decimal(0)
            for allocation in reconciled.allocations:
                pair = (allocation.task_id, allocation.trial)
                if pair in allocated or pair not in scheduled:
                    raise ValueError("AGENT_BILLING_ALLOCATION_PAIR_INVALID")
                allocated.add(pair)
                amount = Decimal(allocation.amount_usd)
                total += amount
                pair_seen[pair].add("agent")
                pair_known[pair]["agent"] = amount
            if allocated != scheduled or total != Decimal(reconciled.amount_usd):
                raise ValueError("AGENT_BILLING_ALLOCATIONS_MUST_COVER_SCHEDULE_AND_TOTAL")
    unavailable = sorted(expected_components - categories_seen)
    matrix_complete = set(grouped) == scheduled and all(len(rows) == 1 for rows in grouped.values())
    successes = sum(
        len(grouped[pair]) == 1
        and bool(grouped[pair][0].get("termination_reason"))
        and grouped[pair][0].get("termination_reason") != "infrastructure_error"
        and _success(_reward(grouped[pair][0]))
        for pair in scheduled
    )
    complete = not missing and not unavailable and matrix_complete
    denominator_complete = matrix_complete and all(
        bool(rows[0].get("termination_reason"))
        and rows[0].get("termination_reason") != "infrastructure_error"
        and type(_reward(rows[0])) in {int, float}
        and math.isfinite(_reward(rows[0]))
        for rows in grouped.values()
    )
    operating = (
        sum(
            (
                known.get(category, Decimal(0))
                for category in ("agent", "agent_provider_billing", "agent_extra", "retrieval")
            ),
            Decimal(0),
        )
        + operating_failed_requests
    )
    expected_pair_categories = expected_components | categories_seen
    if reconciled is not None:
        expected_pair_categories.discard("agent_provider_billing")
        expected_pair_categories.add("agent")
    pair_costs = {}
    for pair in scheduled:
        unknown = (
            (expected_pair_categories - pair_seen[pair])
            | pair_missing[pair]
            | unallocated_components
        )
        pair_costs[pair] = {
            "known_usd_by_category": {
                key: str(value) for key, value in sorted(pair_known[pair].items())
            },
            "known_total_usd": str(sum(pair_known[pair].values(), Decimal(0))),
            "unattributed_or_unknown_components": sorted(unknown),
            "complete": not unknown,
        }
    summary = {
        "model_route": route,
        "upstream_cost_basis": (
            "recorded upstream USD amounts; unknown local alias prices may be zero and do not measure hardware or electricity"
            if route == "local"
            else "recorded upstream USD amounts plus supplied additional ledgers; no independent billing attestation"
        ),
        "agent_operating_numerator_scope": "agent, agent extra, retrieval and attributed failed requests; excludes infrastructure",
        "local_hardware_costs_independently_verified": False,
        "known_usd_by_category": {key: str(value) for key, value in sorted(known.items())},
        "known_total_usd": str(sum(known.values(), Decimal(0))),
        "missing_cost_entries": dict(missing),
        "unreported_components": unavailable,
        "unallocated_additional_components": sorted(unallocated_components),
        "complete": complete,
        "operating_cost_attribution_complete": not operating_attribution_unknown,
        "valid_successful_scheduled_pairs": successes,
        "known_agent_operating_cost_per_success_usd": str(operating / successes)
        if successes
        else None,
        "cost_per_success_denominator": (
            "full_unique_terminal_graded_schedule"
            if denominator_complete
            else "observed_valid_successes_in_incomplete_outcome_matrix"
        ),
        "successful_denominator_complete": denominator_complete,
        "cost_per_success_is_lower_bound": bool(successes)
        and denominator_complete
        and (not complete or operating_attribution_unknown),
        "upstream_agent_reported_usd": str(upstream_agent),
        "upstream_agent_missing_entries": upstream_agent_missing,
        "agent_provider_reconciliation": {
            "present": reconciled is not None,
            "upstream_agent_amount_replaced": reconciled is not None,
            "source_sha256": reconciled.source_sha256 if reconciled else None,
            "provenance": "reviewed supplied ledger; source hash recorded, no provider attestation",
            "provider_attestation_verified": False,
            "task_allocations_complete": reconciled is not None
            and reconciled.allocations is not None,
        },
    }
    return summary, pair_costs


def summarize_saved_results(
    manifest: dict[str, Any],
    simulations: list[dict[str, Any]],
    additional_costs: list[dict[str, Any]] | None = None,
    *,
    episode_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if manifest.get("tau_commit") != TAU_COMMIT:
        raise ValueError("Results belong to a different benchmark revision")
    if manifest.get("kind") != "official_tau3_text":
        raise ValueError("Scripted fixtures cannot be reported as official benchmark runs")
    scheduled = [(str(row["task_id"]), row["trial"]) for row in manifest["scheduled"]]
    if len(scheduled) != len(set(scheduled)):
        raise ValueError("Manifest contains duplicate scheduled task/trial pairs")
    expected = set(scheduled)
    if episode_index is not None:
        validate_episode_index(episode_index, manifest, simulations)
    execution = public_execution(episode_index) if episode_index is not None else None
    execution_rows = {
        (row["task_id"], row["trial"]): row for row in (execution or {}).get("pairs", [])
    }
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
            sources = usage.get("safe_usage_source") or []
            if not isinstance(sources, list):
                sources = [None]
            for source in sources:
                usage_sources[
                    source
                    if isinstance(source, str) and source in {"scripted", "provider_reported"}
                    else "unrecognized"
                ] += 1
    task_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        task_groups[str(row["task_id"])].append(row)
    valid_metric_input = not duplicate_pairs and not unexpected and all(
        type(_reward(row)) in {int, float} and math.isfinite(_reward(row))
        and 0 <= _reward(row) <= 1 for row in eligible
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
            and bool(rows[0].get("termination_reason"))
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

    costs, _ = cost_accounting(manifest, simulations, additional_costs)
    return {
        "run_id": manifest["run_id"],
        "system": manifest["system"],
        "tau_commit": TAU_COMMIT,
        "report_basis": "recorded rewards only; no fresh judging",
        "launch_classification": _launch_classification(manifest),
        "launch_classification_basis": (
            "saved launch metadata; qualification files are not revalidated by reward reporting; "
            "no independent model, endpoint, or reviewer attestation"
        ),
        "run_status": manifest.get("status", "not_recorded"),
        "execution": execution,
        "episode_attribution": {
            "available": episode_index is not None,
            "basis": "explicit task/trial binding with raw result hashes" if episode_index is not None
            else "no bound episode index; missing outcomes are not inferred to be unstarted",
        },
        "model_route_provenance": _route_provenance(manifest),
        "coverage": {
            "scheduled": len(scheduled),
            "recorded_attempts": len(simulations),
            "attempted_pairs": len(set(by_pair) & expected),
            "started_pairs": execution["started"] if execution is not None else None,
            "not_run_pairs": [
                (row["task_id"], row["trial"]) for row in execution["pairs"] if row["state"] == "not_run"
            ] if execution is not None else None,
            "started_without_recorded_outcome_pairs": [
                (row["task_id"], row["trial"]) for row in execution["pairs"] if row["state"] in {"running", "interrupted"}
            ] if execution is not None else None,
            "terminal": sum(bool(row.get("termination_reason")) for row in simulations),
            "graded": len(graded),
            "ungraded": len(ungraded),
            "infrastructure_failures": len(infra),
            "missing_pairs": missing,
            "unexpected_pairs": unexpected,
            "duplicate_pairs": duplicate_pairs,
            "matrix_complete": not (missing or unexpected or duplicate_pairs),
            "grading_complete": not (missing or unexpected or duplicate_pairs or infra or ungraded)
            and all(bool(row.get("termination_reason")) for row in simulations),
        },
        "official_score": {
            "available": bool(valid_metric_input and eligible),
            "avg_reward": sum(_reward(row) for row in eligible) / len(eligible)
            if valid_metric_input and eligible else None,
            "pass_hat_ks": metric_values if valid_metric_input and eligible else None,
            "basis": "saved eligible rewards using the pinned estimator; raw upstream metrics are preserved separately",
            "unavailable_reason": "no_eligible_graded_outcomes" if not eligible
            else "invalid_metric_input" if not valid_metric_input else None,
            "full_schedule_graded": not (missing or unexpected or duplicate_pairs or infra or ungraded),
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
        "pairs": [
            {
                "task_id": task_id, "trial": trial,
                "state": execution_rows[(task_id, trial)]["state"] if execution_rows
                else "recorded" if by_pair.get((task_id, trial)) else "unrecorded_status_unknown",
                "recorded_outcomes": len(by_pair.get((task_id, trial), [])),
                "graded_outcomes": sum(_reward(row) is not None for row in by_pair.get((task_id, trial), [])),
                "termination_reason": by_pair[(task_id, trial)][0].get("termination_reason")
                if len(by_pair.get((task_id, trial), [])) == 1 else None,
                "official_duration_seconds": by_pair[(task_id, trial)][0].get("duration")
                if len(by_pair.get((task_id, trial), [])) == 1
                and by_pair[(task_id, trial)][0].get("termination_reason") != "infrastructure_error" else None,
                "duration_basis": "not_retained_in_infrastructure_placeholder"
                if len(by_pair.get((task_id, trial), [])) == 1
                and by_pair[(task_id, trial)][0].get("termination_reason") == "infrastructure_error"
                else "upstream_recorded_duration",
            }
            for task_id, trial in scheduled
        ],
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
    simulations = results.get("simulations", [])
    report = summarize_saved_results(
        manifest, simulations, costs,
        episode_index=load_episode_index(run_dir, manifest, simulations),
    )
    official_path = run_dir / "official_metrics.json"
    report["official_metrics"] = (
        json.loads(official_path.read_text()) if official_path.exists() else None
    )
    _write_json(run_dir / "report.json", report)
    return report
