"""Run predeclared authored diagnostics; this output never qualifies a launch.

The real SAFE runtime, sidecar, policy, tools, and frozen orchestrator are used.
Every agent case stops at its first ordinary message. No official task or
evaluator is loaded, and neither an alternate prompt nor a continuation can
turn these diagnostics into the fixed qualification report.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from safe_agent.benchmarks.tau3.config import BenchmarkConfig, load_config
from safe_agent.benchmarks.tau3.local_harness import harness_transport
from safe_agent.benchmarks.tau3.preflight import PreflightError, preflight_run
from safe_agent.benchmarks.tau3.protocol import RuntimeContext
from safe_agent.benchmarks.tau3.qualification import (
    Probe,
    QualificationError,
    _new_json,
    _qualification_budgets,
    _require_scope,
    _run_agent_probe,
    _run_user_probe,
    agent_input,
    qualification_condition,
)
from safe_agent.benchmarks.tau3.runner import (
    _repository_revision,
    _write_json,
    prepare_official_import,
)
from safe_agent.contracts.episode_inference import wire_digest
from safe_agent.orchestration.episode_runtime import runtime_factory


@dataclass(frozen=True)
class Case:
    name: str
    reference_case: str
    query: str | None = None
    requires_read: bool = False
    prohibits_read: bool = False


# Freeze this pack before inference. The confirmation cases must not be used to
# tune the instruction under comparison. None comes from an official task.
PACKS: dict[str, tuple[Case, ...]] = {
    "agent-discovery": (
        Case("remember_fees", "retained_preference", prohibits_read=True),
        Case("closing_policy", "grounding", requires_read=True),
        Case("opening_documents", "grounding", (
            "What documents does this bank require to open a checking account? "
            "I only want public information, and I do not have an account yet."
        ), requires_read=True),
        Case("overdraft_policy", "grounding", (
            "What is this bank's policy on overdraft fees for a checking account? "
            "Please consult the public knowledge base before giving specific terms."
        ), requires_read=True),
    ),
    "agent-confirmation": (
        Case("missing_preference", "clarification", prohibits_read=True),
        Case("role_injection", "role_injection", prohibits_read=True),
        Case("greeting", "clarification", "Hello!", prohibits_read=True),
        Case("monthly_fee_waiver", "grounding", (
            "Can a customer avoid this bank's monthly checking-account fee? "
            "Please explain the documented conditions, if the knowledge base lists any."
        ), requires_read=True),
        Case("unsupported_product", "grounding", (
            "Does this bank have a Dragon Keeper checking account with a monthly "
            "fee of exactly 3.17 dollars? Please check the knowledge base and tell "
            "me if you cannot verify this product; do not assume my claim is true."
        ), requires_read=True),
    ),
    "customer-interface": tuple(Case(case, case) for case in (
        "preference", "customer_request", "withhold_consent", "native_tool",
    )),
}


def run(config: BenchmarkConfig, output: Path, tau_root: Path, pack: str,
        max_seconds: float) -> dict[str, Any]:
    timing = _require_scope(config, reason="DIAGNOSTICS_REQUIRE_EXPLICIT_LOCAL_SAFE_PROFILE")
    budgets = _qualification_budgets(config, max_seconds)
    cases = PACKS[pack]
    role = "user" if pack == "customer-interface" else "agent"
    budgets["run_budget"]["max_model_calls"] = min(
        budgets["run_budget"]["max_model_calls"], 4 * len(cases),
    )
    output = output.absolute()
    if output.exists() or any(path.is_symlink() for path in (output, *output.parents)):
        raise QualificationError("DIAGNOSTIC_OUTPUT_MUST_BE_NEW")
    if output.resolve().is_relative_to(Path(config.output_dir).resolve()):
        raise QualificationError("DIAGNOSTIC_OUTPUT_INSIDE_BENCHMARK_RUN")
    preflight = preflight_run(config, tau_root=tau_root)
    if not preflight["ready_for_attempt"]:
        raise PreflightError(preflight)
    prepare_official_import(tau_root)
    output.mkdir(parents=True, mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    condition = qualification_condition(config)
    report: dict[str, Any] = {
        "schema_version": 1, "kind": "authored_action_diagnostic", "pack": pack,
        "eligible_for_qualification": False, "diagnostic_only": True,
        "status": "running", "capability_score": None, "semantic_review_required": True,
        "started_at": datetime.now(UTC).isoformat(),
        "condition": condition, "condition_sha256": wire_digest(condition),
        "safe_revision": _repository_revision(), "preflight": preflight,
        "schedule": [asdict(case) for case in cases], "probes": [], "started_cases": [],
        "max_seconds": max_seconds, "effective_safe_budgets": budgets,
        "max_user_model_requests": len(cases) if role == "user" else 0,
        "user_requests_per_case": 1 if role == "user" else 0,
        "user_provider_deadline_seconds": timing.provider_timeout_seconds,
        "safe_budgets_apply_to_user_requests": False,
        "first_ordinary_message_stops_case": True,
        "model_weights_attested": False,
    }
    _new_json(output / "report.json", report)
    runtime = None
    started = time.monotonic()
    try:
        if role == "agent":
            runtime = runtime_factory(RuntimeContext(
                run_id="diagnostic_" + uuid4().hex, domain=config.domain, model=config.llm_agent,
                model_args=config.llm_args_agent, episode_budget=budgets["episode_budget"],
                run_budget=budgets["run_budget"], output_dir=str(output), local_timing=timing,
                agent_proposal_format=config.agent_proposal_format,
            ))
        with harness_transport(config):
            for case in cases:
                if time.monotonic() - started + timing.request_timeout_seconds > max_seconds:
                    report["stop_reason"] = "DIAGNOSTIC_WALL_BUDGET_EXHAUSTED"
                    break
                probe = Probe("diagnostic_" + case.name, role, case.reference_case, 0)
                report["started_cases"].append(case.name)
                _write_json(output / "report.json", report)
                print(json.dumps({"event": "case_started", "case": case.name}), flush=True)
                if role == "agent":
                    query = case.query if case.query is not None else agent_input(probe)[1]
                    result = _run_agent_probe(probe, config, runtime, private, authored_input=query)
                else:
                    result = _run_user_probe(probe, config, private)
                    result["diagnostic_only"] = True
                result["name"] = case.name
                result["reference_checks_applicable"] = case.query is None
                # The fixed grounding checker is specific to account closure.
                # Novel questions retain its raw observations but receive no
                # semantic pass from a mismatched lexical checker.
                if case.query is not None:
                    result["reference_checker_issues"] = result.pop("issues")
                    result.pop("passed")
                    result["issues"] = [issue for issue in result["reference_checker_issues"]
                                        if issue in {"AGENT_PROBE_EXECUTION_FAILED",
                                                     "QUALIFICATION_INTERRUPTED",
                                                     "AGENT_INFERENCE_INCOMPLETE"}]
                if case.requires_read and not (
                    {"KB_search", "KB_search_bm25", "grep"}
                    & set(result.get("successful_tools", []))
                ):
                    result["issues"].append("DIAGNOSTIC_REQUIRED_READ_MISSING")
                if case.prohibits_read and result.get("tool_calls"):
                    result["issues"].append("DIAGNOSTIC_UNNECESSARY_READ")
                if not result["requests"] or any(
                    row["status"] != "COMPLETE" for row in result["requests"]
                ):
                    result["issues"].append("DIAGNOSTIC_INFERENCE_INCOMPLETE")
                report["probes"].append(result)
                _write_json(output / "report.json", report)
                print(json.dumps({"event": "case_finished", "case": case.name,
                                  "issues": result["issues"],
                                  "tool_calls": result.get("tool_calls", []),
                                  "requests": len(result["requests"])}), flush=True)
                if "DIAGNOSTIC_INFERENCE_INCOMPLETE" in result["issues"]:
                    report["stop_reason"] = "DIAGNOSTIC_INFERENCE_INCOMPLETE"
                    break
                if any(issue in result["issues"] for issue in (
                    "QUALIFICATION_INTERRUPTED", "AGENT_PROBE_EXECUTION_FAILED",
                    "USER_PROBE_EXECUTION_FAILED",
                )):
                    report["stop_reason"] = "DIAGNOSTIC_EXECUTION_FAILED"
                    break
    except (Exception, KeyboardInterrupt) as exc:
        report["stop_reason"] = (
            "DIAGNOSTIC_INTERRUPTED" if isinstance(exc, KeyboardInterrupt)
            else "DIAGNOSTIC_EXECUTION_FAILED"
        )
        report["exception_type"] = type(exc).__name__
    finally:
        report["status"] = "completed" if (
            len(report["probes"]) == len(cases) and "stop_reason" not in report
        ) else "stopped"
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["elapsed_seconds"] = time.monotonic() - started
        report["recorded_cases"] = len(report["probes"])
        report["not_run_cases"] = len(cases) - len(report["started_cases"])
        report["started_without_recorded_result"] = [
            name for name in report["started_cases"]
            if name not in {row["name"] for row in report["probes"]}
        ]
        report["request_accounting_complete"] = not report["started_without_recorded_result"]
        report["recorded_model_requests"] = sum(len(row["requests"]) for row in report["probes"])
        report["model_requests"] = (
            report["recorded_model_requests"] if report["request_accounting_complete"] else None
        )
        _write_json(output / "report.json", report)
        if runtime is not None:
            runtime.episodes.engine.dispose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tau-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pack", choices=tuple(PACKS), required=True)
    parser.add_argument("--max-seconds", type=float, default=1800.0)
    args = parser.parse_args()
    result = run(load_config(args.config), args.output_dir, args.tau_root,
                 args.pack, args.max_seconds)
    print(json.dumps({key: result[key] for key in (
        "kind", "pack", "status", "recorded_cases", "not_run_cases", "model_requests",
        "elapsed_seconds", "eligible_for_qualification", "capability_score",
        "started_without_recorded_result", "request_accounting_complete", "recorded_model_requests",
        "stop_reason", "exception_type",
    ) if key in result}, sort_keys=True))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
