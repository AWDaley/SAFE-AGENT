"""Offline, content-verified inspection of the frozen text benchmark.

This module reads evaluator-side task files only to produce IDs, hashes, counts,
and grading metadata. Never pass its raw task dictionaries to an agent.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import EXPECTED_TASK_COUNTS, TAU_COMMIT, TAU_RELEASE, TAU_REPOSITORY
from .config import BenchmarkConfig


def content_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def pinned_source_manifest() -> dict[str, Any]:
    resource = files("safe_agent.benchmarks.tau3").joinpath("source_manifest.json")
    result = json.loads(resource.read_text(encoding="utf-8"))
    if result["commit"] != TAU_COMMIT:
        raise ValueError("Bundled source manifest does not match the benchmark pin")
    return result


def verify_sources(tau_root: Path, domain: str) -> dict[str, Any]:
    manifest = pinned_source_manifest()
    required = {
        path: digest
        for path, digest in manifest["files"].items()
        if path.startswith("src/tau2/")
        or path == "pyproject.toml"
        or path.startswith(f"data/tau2/domains/{domain}/")
    }
    missing, modified, unsafe = [], [], []
    root = tau_root.resolve()
    for relative, expected in required.items():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
        elif not path.resolve().is_relative_to(root):
            unsafe.append(relative)
        elif git_blob_hash(path.read_bytes()) != expected:
            modified.append(relative)
    # Extra task files would silently enlarge the official banking loader's set.
    task_dir = root / "data/tau2/domains/banking_knowledge/tasks"
    extras = []
    if domain == "banking_knowledge" and task_dir.is_dir():
        extras = sorted(
            str(path.relative_to(root))
            for path in task_dir.glob("task_*.json")
            if str(path.relative_to(root)) not in required
        )
    return {
        "verified": not (missing or modified or unsafe or extras),
        "required_files": len(required),
        "verified_files": len(required) - len(missing) - len(modified) - len(unsafe),
        "missing_files": sorted(missing),
        "modified_files": sorted(modified),
        "unsafe_paths": sorted(unsafe),
        "unexpected_task_files": extras,
        "manifest_sha256": content_hash(manifest),
    }


def _read_tasks(tau_root: Path, domain: str) -> list[dict[str, Any]]:
    directory = tau_root / "data/tau2/domains" / domain
    if domain == "banking_knowledge":
        return [
            json.loads(p.read_text()) for p in sorted((directory / "tasks").glob("task_*.json"))
        ]
    value = json.loads((directory / "tasks.json").read_text())
    if not isinstance(value, list):
        raise ValueError("Expected the pinned retail task array")
    return value


def selected_task_ids(tau_root: Path, config: BenchmarkConfig) -> list[str]:
    tasks = _read_tasks(tau_root, config.domain)
    ids = [str(task["id"]) for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate IDs in task data")
    if config.domain == "retail":
        path = tau_root / "data/tau2/domains/retail/split_tasks.json"
        splits = json.loads(path.read_text())
        allowed = set(map(str, splits[config.task_split_name]))
        ids = [task_id for task_id in ids if task_id in allowed]
    if config.task_ids is not None:
        if set(config.task_ids) - set(ids):
            raise ValueError("Requested task IDs do not exist in the selected split")
        ids = [task_id for task_id in ids if task_id in set(config.task_ids)]
    if config.num_tasks is not None:
        ids = ids[: config.num_tasks]
    return ids


def _tool_inventory(path: Path) -> dict[str, list[str]]:
    """List decorated methods without importing or executing the benchmark."""
    found: dict[str, list[str]] = {}
    tree = ast.parse(path.read_text())
    for cls in (item for item in tree.body if isinstance(item, ast.ClassDef)):
        for method in (item for item in cls.body if isinstance(item, ast.FunctionDef)):
            for decorator in method.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Name) and target.id in {
                    "is_tool",
                    "is_discoverable_tool",
                }:
                    key = f"{cls.name}:{target.id}"
                    found.setdefault(key, []).append(method.name)
    return {key: sorted(value) for key, value in sorted(found.items())}


def reservation_headroom(
    config: BenchmarkConfig, scheduled_conversations: int | None = None
) -> dict[str, Any]:
    """Explain the illustrative fixed holds, without estimating provider invoices."""
    report: dict[str, Any] = {
        "applies_to_selected_system": config.system == "safe_agent",
        "basis": "fresh run with zero prior usage; configured conservative cost holds only",
        "invoice_estimate": False,
        "scheduled_conversations": scheduled_conversations,
        "calculation_available": False,
    }
    if config.system != "safe_agent":
        report["note"] = (
            "SAFE budget sections are ignored by stock LLMAgent; external billing is not capped here"
        )
        return report
    hold = config.episode_budget.get("max_attempt_cost_microusd")
    episode_cap = config.episode_budget.get("max_cost_microusd")
    run_cap = config.run_budget.get("max_cost_microusd")
    if not (
        type(hold) is int
        and hold > 0
        and type(episode_cap) is int
        and episode_cap >= 0
        and type(run_cap) is int
        and run_cap >= 0
    ):
        report["note"] = "Explicit valid cost-hold limits are required for this offline calculation"
        return report
    episode_attempts = episode_cap // hold
    run_attempts = run_cap // hold
    matrix_attempts = (
        min(run_attempts, episode_attempts * scheduled_conversations)
        if scheduled_conversations is not None
        else None
    )
    report.update(
        {
            "calculation_available": True,
            "attempt_hold_microusd": hold,
            "episode_hold_limit_microusd": episode_cap,
            "run_hold_limit_microusd": run_cap,
            "episode_attempt_ceiling_from_holds": episode_attempts,
            "run_attempt_ceiling_from_holds": run_attempts,
            "scheduled_matrix_attempt_ceiling_from_holds": matrix_attempts,
            "mean_attempt_ceiling_per_conversation": (
                matrix_attempts / scheduled_conversations
                if matrix_attempts is not None and scheduled_conversations
                else None
            ),
            "note": (
                "Pilot-calibrate these illustrative limits before coverage/final runs. "
                "Every reserved attempt consumes its hold, including malformed replies and uncertainty. "
                "Other limits can reduce this ceiling; it is not expected model usage or an invoice. "
                "Stock, user-simulator, and judge spending are outside these SAFE limits."
            ),
        }
    )
    return report


def inspect_benchmark(config: BenchmarkConfig, tau_root: Path | None = None) -> dict[str, Any]:
    root = tau_root or (Path(config.tau_root) if config.tau_root else None)
    report: dict[str, Any] = {
        "repository": TAU_REPOSITORY,
        "release": TAU_RELEASE,
        "commit": TAU_COMMIT,
        "domain": config.domain,
        "retrieval_config": config.retrieval_config,
        "expected_domain_task_count": EXPECTED_TASK_COUNTS[config.domain],
        "stage": config.stage,
        "source_verified": False,
        "model_calls": 0,
        "status": "missing_source",
        "safe_reservation_headroom": reservation_headroom(config),
    }
    if root is None or not root.is_dir():
        report["issues"] = ["Supply a local pinned checkout using --tau-root or tau_root in TOML"]
        return report
    verification = verify_sources(root, config.domain)
    report["source_verification"] = verification
    report["source_verified"] = verification["verified"]
    if not verification["verified"]:
        report["status"] = "source_mismatch"
        return report
    tasks = _read_tasks(root, config.domain)
    ids = selected_task_ids(root, config)
    basis = Counter(
        tuple(task.get("evaluation_criteria", {}).get("reward_basis", [])) for task in tasks
    )
    selected = [task for task in tasks if str(task["id"]) in set(ids)]
    selected_basis = Counter(
        tuple(task.get("evaluation_criteria", {}).get("reward_basis", [])) for task in selected
    )
    report.update(
        {
            "status": "ready",
            "domain_task_count": len(tasks),
            "selected_task_ids": ids,
            "selected_task_count": len(ids),
            "task_ids_sha256": content_hash(ids),
            "task_content_sha256": content_hash(tasks),
            "grading_basis_counts": {"+".join(key): value for key, value in sorted(basis.items())},
            "selected_grading_basis_counts": {
                "+".join(key): value for key, value in sorted(selected_basis.items())
            },
            "selected_tasks_with_nl_assertions": sum(
                bool(task.get("evaluation_criteria", {}).get("nl_assertions")) for task in selected
            ),
            "scheduled_conversations": len(ids) * config.num_trials,
            "safe_reservation_headroom": reservation_headroom(config, len(ids) * config.num_trials),
            "num_trials": config.num_trials,
            "tool_inventory": _tool_inventory(root / f"src/tau2/domains/{config.domain}/tools.py"),
        }
    )
    if len(tasks) != EXPECTED_TASK_COUNTS[config.domain]:
        raise ValueError("The content-verified task count does not match the frozen release")
    if config.domain == "banking_knowledge":
        report["knowledge_document_count"] = len(
            list((root / "data/tau2/domains/banking_knowledge/documents").glob("*.json"))
        )
        report["held_out_split"] = False
    else:
        report["held_out_split"] = config.task_split_name == "test"
    return report
