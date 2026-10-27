"""Same-process SAFE registration and the unchanged pinned τ text runner."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from . import TAU_COMMIT
from .adapter import official_agent_class
from .config import CANONICAL_RUNTIME_FACTORY, BenchmarkConfig, harness_model_settings
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
    lifecycle: Callable[[str, str, dict[str, int]], None] | None = None,
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
            local_timing=config.local_timing,
        )
        return agent_class(
            tools=tools,
            domain_policy=domain_policy,
            domain=config.domain,
            runtime=factory(context),
            lifecycle=lifecycle,
        )

    registry.register_agent_factory(create_agent, registration_name, metadata={"solo_mode": False})
    return registration_name


def text_run_config(config: BenchmarkConfig, *, agent_name: str, output_dir: Path) -> Any:
    from tau2.data_model.simulation import TextRunConfig  # type: ignore[import-untyped]

    agent_model, agent_args = harness_model_settings(config, "agent")
    user_model, user_args = harness_model_settings(config, "user")
    values = dict(
        domain=config.domain,
        agent=agent_name,
        user="user_simulator",
        task_split_name=config.task_split_name,
        task_ids=config.task_ids,
        num_tasks=config.num_tasks,
        num_trials=config.num_trials,
        llm_agent=agent_model,
        llm_args_agent=agent_args,
        llm_user=user_model,
        llm_args_user=user_args,
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
    qualification_evidence: dict[str, Any] | None = None,
) -> Path:
    """Execute an explicit official run. This function may make billed model calls.

    Inspection and manifest preparation happen before any agent is constructed.
    No retry, filtering, automatic review, or rejudging is added by this wrapper.
    The explicit development infrastructure-stop setting selects a SAFE serial
    schedule around the pinned per-task runner; the default batch path is unchanged.
    """
    root = tau_root or (Path(config.tau_root) if config.tau_root else None)
    if qualification_evidence is not None:
        expected_keys = {"report_sha256", "review_sha256", "condition_sha256", "review_is_independent_attestation"}
        if (
            set(qualification_evidence) != expected_keys
            or any(not isinstance(qualification_evidence.get(name), str)
                   or re.fullmatch(r"[0-9a-f]{64}", qualification_evidence[name]) is None
                   for name in ("report_sha256", "review_sha256"))
            or not isinstance(qualification_evidence.get("condition_sha256"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", qualification_evidence["condition_sha256"]) is None
            or qualification_evidence["review_is_independent_attestation"] is not False
        ):
            raise ValueError("QUALIFICATION_GATE_METADATA_INVALID")
    if runtime_factory is not None and config.stage != "development":
        raise ValueError("Injected runtime factories are restricted to labelled development runs")
    inventory = inspect_benchmark(config, root)
    if inventory["status"] != "ready" or root is None:
        raise RuntimeError("Official run requires a complete, content-verified pinned τ checkout")
    preflight: dict[str, Any] | None = None
    if runtime_factory is None and config.runtime_factory == CANONICAL_RUNTIME_FACTORY:
        from .preflight import PreflightError, preflight_run

        preflight = preflight_run(config, tau_root=root)
        if not preflight["ready_for_attempt"]:
            # No run directory, official simulator, budget hold, or provider
            # dispatch exists when launch configuration is rejected here.
            raise PreflightError(preflight)
    if config.model_route == "local" and (
        config.stage != "development" or inventory["selected_tasks_with_nl_assertions"]
    ):
        # Injected/custom fixtures also cannot turn the local route into an
        # implicit hosted-judge run, even though they skip canonical preflight.
        raise ValueError("LOCAL_ROUTE_REQUIRES_DEVELOPMENT_TASKS_WITHOUT_HOSTED_JUDGE")
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
        "preflight": preflight,
        "preflight_sha256": content_hash(preflight) if preflight is not None else None,
        "billing_cap_scope": "SAFE durable reservation limits; external billed spend is not independently capped/verified",
        "expected_cost_components": ["agent", "user"]
        + (["infrastructure"] if config.model_route == "local" else
           ["agent_provider_billing"] if config.system == "safe_agent" else [])
        + (["judge"] if inventory["selected_tasks_with_nl_assertions"] else []),
        "model_route": config.model_route,
        "local_api_base": config.local_api_base,
        "local_execution_attested": False,
        "model_weights_attested": False,
    }
    if config.model_route == "local":
        manifest["billing_cap_scope"] = (
            "Local endpoint development condition; SAFE reservation limits do not measure "
            "hardware, electricity, or server operating costs"
        )
    manifest_path = output_dir / "manifest.json"
    episode_index = None
    previous_umask = os.umask(0o077)
    try:
        if qualification_evidence is not None:
            # CLI validation has already bound these digests to the requested
            # condition. Persist the reviewed fixed metadata before constructing
            # any official agent; no reviewer identity or review prose is copied.
            manifest["qualification_gate"] = dict(qualification_evidence)
            _write_json(output_dir / "qualification-gate.json", qualification_evidence)
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
        if config.stop_after_infrastructure_errors is not None:
            from .execution import SCHEDULER, EpisodeIndex

            episode_index = EpisodeIndex(output_dir, manifest)
            manifest["scheduler"] = SCHEDULER
            manifest["stop_after_infrastructure_errors"] = config.stop_after_infrastructure_errors
        else:
            manifest["scheduler"] = "pinned_upstream_batch"
        agent_name: str = config.system
        if config.system == "safe_agent":
            agent_name = register_safe_agent(
                config, output_dir=output_dir, runtime_factory=runtime_factory,
                lifecycle=episode_index.lifecycle if episode_index is not None else None,
            )
        official_config = text_run_config(config, agent_name=agent_name, output_dir=output_dir)
        manifest["official_config"] = official_config.model_dump(mode="json")
        manifest["status"] = "running"
        _write_json(manifest_path, manifest)
        from tau2.runner import run_domain  # type: ignore[import-untyped]

        from .local_harness import harness_transport

        with harness_transport(config):
            if episode_index is None:
                results = run_domain(official_config)
            else:
                from .execution import run_controlled

                assert config.stop_after_infrastructure_errors is not None
                results = run_controlled(
                    official_config, output_dir=output_dir, index=episode_index,
                    error_limit=config.stop_after_infrastructure_errors,
                )
        # run_domain saves its own unchanged Results. Store official metric
        # output as well; this reads saved rewards and never invokes a judge.
        from tau2.metrics.agent_metrics import compute_metrics  # type: ignore[import-untyped]

        _write_json(output_dir / "official_metrics.json", compute_metrics(results).model_dump())
        manifest["saved_artifacts_sha256"] = {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            for name in ("results.json", "official_metrics.json")
        }
        if episode_index is None:
            manifest["status"] = "complete"
        else:
            manifest.update(episode_index.manifest_fields())
    except BaseException as exc:
        manifest["status"] = (
            "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        )
        manifest["error_type"] = type(exc).__name__
        if episode_index is not None:
            episode_index.finalize(
                manifest["status"],
                "keyboard_interrupt" if isinstance(exc, KeyboardInterrupt)
                else "system_exit" if isinstance(exc, SystemExit) else "runner_failure",
            )
            manifest.update(episode_index.manifest_fields())
        raise
    finally:
        try:
            manifest["finished_at"] = datetime.now(UTC).isoformat()
            _write_json(manifest_path, manifest)
        finally:
            os.umask(previous_umask)
    return output_dir
