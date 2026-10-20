"""Same-process SAFE registration and the unchanged pinned τ text runner."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import TAU_COMMIT
from .adapter import official_agent_class
from .config import CANONICAL_RUNTIME_FACTORY, BenchmarkConfig
from .inventory import content_hash, inspect_benchmark
from .protocol import RuntimeContext, RuntimeFactory


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def prepare_official_import(tau_root: Path) -> None:
    """Load only the already content-verified checkout and its own data."""
    root = tau_root.resolve()
    loaded = sys.modules.get("tau2")
    expected_package = root / "src/tau2"
    loaded_file = getattr(loaded, "__file__", None)
    if loaded is not None and (
        loaded_file is None or Path(loaded_file).resolve().parent != expected_package
    ):
        raise RuntimeError("A different tau2 package is already loaded; start a fresh process")
    os.environ["TAU2_DATA_DIR"] = str(root / "data")
    source_path = str(root / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    package = importlib.import_module("tau2")
    if package.__file__ is None or Path(package.__file__).resolve().parent != expected_package:
        raise RuntimeError("The imported tau2 package does not match the verified source")
    from tau2.utils.utils import DATA_DIR  # type: ignore[import-untyped]

    if DATA_DIR.resolve() != root / "data":
        raise RuntimeError("τ data was initialized from another directory; start a fresh process")


def _load_runtime_factory(reference: str) -> RuntimeFactory:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute or "." in attribute:
        raise ValueError("runtime_factory must be a trusted module:callable reference")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise ValueError("runtime_factory does not name a callable")
    return value


def register_safe_agent(
    config: BenchmarkConfig,
    *,
    output_dir: Path,
    runtime_factory: RuntimeFactory | None = None,
) -> str:
    from tau2.registry import registry  # type: ignore[import-untyped]

    factory = runtime_factory or _load_runtime_factory(config.runtime_factory)
    agent_class = official_agent_class()
    registration_name = "safe_agent_" + config.run_id

    def create_agent(
        tools: list[Any],
        domain_policy: str,
        *,
        llm: str | None = None,
        llm_args: dict[str, Any] | None = None,
        task: Any = None,
        **unused: Any,
    ) -> Any:
        # Task includes hidden user instructions, expected actions, required
        # documents, and evaluator data. It must not enter the runtime factory.
        del task, unused
        context = RuntimeContext(
            run_id=config.run_id,
            domain=config.domain,
            model=llm or config.llm_agent,
            model_args=llm_args if llm_args is not None else config.llm_args_agent,
            episode_budget=config.episode_budget,
            run_budget=config.run_budget,
            output_dir=str(output_dir),
        )
        return agent_class(
            tools=tools,
            domain_policy=domain_policy,
            domain=config.domain,
            runtime=factory(context),
        )

    registry.register_agent_factory(create_agent, registration_name, metadata={"solo_mode": False})
    return registration_name


def text_run_config(config: BenchmarkConfig, *, agent_name: str, output_dir: Path) -> Any:
    from tau2.data_model.simulation import TextRunConfig  # type: ignore[import-untyped]

    values = dict(
        domain=config.domain,
        agent=agent_name,
        user="user_simulator",
        task_split_name=config.task_split_name,
        task_ids=config.task_ids,
        num_tasks=config.num_tasks,
        num_trials=config.num_trials,
        llm_agent=config.llm_agent,
        llm_args_agent=dict(config.llm_args_agent),
        llm_user=config.llm_user,
        llm_args_user=dict(config.llm_args_user),
        max_steps=config.max_steps,
        max_errors=config.max_errors,
        max_concurrency=1,
        max_retries=0,
        seed=config.seed,
        timeout=config.timeout,
        auto_resume=False,
        auto_review=False,
        log_level="INFO",
        verbose_logs=False,
        enforce_communication_protocol=True,
        save_to=str(output_dir.resolve()),
    )
    if config.domain == "banking_knowledge":
        values.update(
            retrieval_config="bm25", retrieval_config_kwargs=config.retrieval_config_kwargs
        )
    return TextRunConfig(**values)


def _repository_revision() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[4]
    source_hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "src/safe_agent").rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".toml", ".lock"}
    }
    fingerprint = content_hash(source_hashes)
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        return {"commit": revision, "dirty": dirty, "runtime_sources_sha256": fingerprint}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "runtime_sources_sha256": fingerprint}


def run_official(
    config: BenchmarkConfig,
    *,
    tau_root: Path | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> Path:
    """Execute an explicit official run. This function may make billed model calls.

    Inspection and manifest preparation happen before any agent is constructed.
    No retry, filtering, automatic review, or rejudging is added by this wrapper.
    """
    root = tau_root or (Path(config.tau_root) if config.tau_root else None)
    if runtime_factory is not None and config.stage != "development":
        raise ValueError("Injected runtime factories are restricted to labelled development runs")
    inventory = inspect_benchmark(config, root)
    if inventory["status"] != "ready" or root is None:
        raise RuntimeError("Official run requires a complete, content-verified pinned τ checkout")
    output_dir = Path(config.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("Run directory is not empty; choose a new run ID/output directory")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    runtime_mode = (
        "injected_development"
        if runtime_factory is not None
        else "custom_development"
        if config.runtime_factory != CANONICAL_RUNTIME_FACTORY
        else "canonical_safe"
        if config.system == "safe_agent"
        else "stock_baseline"
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "official_tau3_text",
        "tau_commit": TAU_COMMIT,
        "tau_root": str(root.resolve()),
        "run_id": config.run_id,
        "system": config.system
        if runtime_mode in {"canonical_safe", "stock_baseline"}
        else runtime_mode,
        "runtime_mode": runtime_mode,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "preparing",
        "config": config.model_dump(),
        "config_sha256": content_hash(config.model_dump()),
        "safe_revision": _repository_revision(),
        "inventory": inventory,
        "scheduled": [
            {"task_id": task_id, "trial": trial}
            for task_id in inventory["selected_task_ids"]
            for trial in range(config.num_trials)
        ],
        "official_results": str(output_dir / "results.json"),
        "evaluation_type": "ALL",
        "automatic_review": False,
        "retries": 0,
        "billing_cap_scope": "SAFE durable reservation limits; external billed spend is not independently capped/verified",
        "expected_cost_components": ["agent", "user"]
        + (["agent_provider_billing"] if config.system == "safe_agent" else [])
        + (["judge"] if inventory["selected_tasks_with_nl_assertions"] else []),
    }
    manifest_path = output_dir / "manifest.json"
    previous_umask = os.umask(0o077)
    try:
        _write_json(manifest_path, manifest)
        prepare_official_import(root)
        from tau2.config import (  # type: ignore[import-untyped]
            DEFAULT_LLM_NL_ASSERTIONS,
            DEFAULT_LLM_NL_ASSERTIONS_ARGS,
        )

        manifest["judge_model"] = DEFAULT_LLM_NL_ASSERTIONS
        manifest["judge_args"] = DEFAULT_LLM_NL_ASSERTIONS_ARGS
        manifest["dependencies"] = {
            package: importlib.metadata.version(package)
            for package in ("tervoro-safe-agent", "tau2", "pydantic", "litellm", "rank-bm25")
        }
        agent_name: str = config.system
        if config.system == "safe_agent":
            agent_name = register_safe_agent(
                config, output_dir=output_dir, runtime_factory=runtime_factory
            )
        official_config = text_run_config(config, agent_name=agent_name, output_dir=output_dir)
        manifest["official_config"] = official_config.model_dump(mode="json")
        manifest["status"] = "running"
        _write_json(manifest_path, manifest)
        from tau2.runner import run_domain  # type: ignore[import-untyped]

        results = run_domain(official_config)
        # run_domain saves its own unchanged Results. Store official metric
        # output as well; this reads saved rewards and never invokes a judge.
        from tau2.metrics.agent_metrics import compute_metrics  # type: ignore[import-untyped]

        _write_json(output_dir / "official_metrics.json", compute_metrics(results).model_dump())
        manifest["saved_artifacts_sha256"] = {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            for name in ("results.json", "official_metrics.json")
        }
        manifest["status"] = "complete"
    except BaseException as exc:
        manifest["status"] = (
            "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        )
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            manifest["finished_at"] = datetime.now(UTC).isoformat()
            _write_json(manifest_path, manifest)
        finally:
            os.umask(previous_umask)
    return output_dir
