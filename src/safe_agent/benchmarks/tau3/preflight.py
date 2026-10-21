"""No-inference launch checks for the frozen hosted benchmark condition.

An authenticated local capability exchange proves a matching sidecar
configuration. It does not test provider credentials, quota, or model access.
Nothing here constructs an official agent, opens a run database, or reserves
inference budget. Offline mode does not read dispatch keys or contact a socket.
"""

from __future__ import annotations

import ast
import importlib.metadata
import math
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from safe_agent.contracts.episode_inference import EpisodeInferenceRequest

from . import TAU_COMMIT, TAU_RELEASE
from .config import CANONICAL_RUNTIME_FACTORY, BenchmarkConfig
from .inventory import content_hash, inspect_benchmark, reservation_headroom


class PreflightError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__("BENCHMARK_PREFLIGHT_BLOCKED")
        self.report = report


def _runtime_configuration(config: BenchmarkConfig) -> dict[str, Any]:
    from safe_agent.orchestration.episode_runtime import RunLimits, RuntimeLimits

    episode = RuntimeLimits.model_validate(config.episode_budget)
    run = RunLimits.model_validate(config.run_budget)
    # Also apply the durable contract's upper bounds before any database exists.
    episode_limits = episode.episode_limits()
    run_limits = run.episode_limits()
    if any(getattr(episode_limits, name) > getattr(run_limits, name)
           for name in type(episode_limits).model_fields):
        raise ValueError("EPISODE_LIMIT_EXCEEDS_RUN_LIMIT")
    if set(config.llm_args_agent) - {"temperature", "seed"}:
        raise ValueError("UNSUPPORTED_EPISODE_MODEL_ARGUMENTS")
    now = datetime.now(UTC)
    request = EpisodeInferenceRequest(
        request_id="preflight_validation", episode_id="preflight_validation",
        domain=config.domain, model=config.llm_agent,
        policy_hash="sha256:" + "0" * 64,
        observation_hash="sha256:" + "0" * 64,
        projected_context={}, max_output_tokens=episode.max_output_tokens,
        temperature=config.llm_args_agent.get("temperature", 0.0),  # type: ignore[arg-type]
        seed=config.llm_args_agent.get("seed"),  # type: ignore[arg-type]
        issued_at=now, deadline=now + timedelta(seconds=30),
    )
    # Use the smallest valid envelope, including the mandatory fields. Actual
    # runtime IDs, timestamps, and projected context require additional units.
    minimum_request = request.model_copy(update={
        "request_id": "x", "episode_id": "x", "seed": 0,
        "issued_at": datetime(2000, 1, 1, tzinfo=UTC),
        "deadline": datetime(2000, 1, 1, 0, 0, 1, tzinfo=UTC),
    })
    minimum_hold = len(minimum_request.model_dump_json().encode()) + 8192 + episode.max_output_tokens
    return {"episode_budget": episode.model_dump(), "run_budget": run.model_dump(),
            "minimum_token_hold": minimum_hold,
            "minimum_token_hold_basis": "smallest valid empty-context request; actual prompts require more"}


def _harness_arguments(arguments: dict[str, Any]) -> None:
    # This first launch profile deliberately covers the checked-in OpenAI
    # condition, without accepting hidden credentials, endpoint overrides,
    # callbacks, mock replies, or custom provider routes in model arguments.
    if set(arguments) - {"temperature", "seed", "num_retries", "max_tokens"}:
        raise ValueError("UNSUPPORTED_HARNESS_MODEL_ARGUMENTS")
    temperature = arguments.get("temperature", 0.0)
    if (type(temperature) not in {int, float} or not math.isfinite(temperature)
            or not 0 <= temperature <= 2):
        raise ValueError("INVALID_HARNESS_MODEL_ARGUMENTS")
    seed = arguments.get("seed")
    if seed is not None and type(seed) is not int:
        raise ValueError("INVALID_HARNESS_MODEL_ARGUMENTS")
    retries = arguments.get("num_retries")
    if retries is not None and (type(retries) is not int or not 0 <= retries <= 20):
        raise ValueError("INVALID_HARNESS_MODEL_ARGUMENTS")
    tokens = arguments.get("max_tokens")
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ValueError("INVALID_HARNESS_MODEL_ARGUMENTS")


def _reported_retries(arguments: dict[str, Any], default: int) -> int | None:
    value = arguments.get("num_retries")
    if value is None:
        return default
    # Invalid model arguments block launch but must not echo arbitrary values.
    return value if type(value) is int and 0 <= value <= 20 else None


def _openai_model(model: str) -> bool:
    # Routing syntax only. A name passing this check may still be unavailable
    # to the account; preflight never contacts the provider to find out.
    return bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", model)) and model.startswith(
        ("gpt-", "chatgpt-", "o1", "o3", "o4", "ft:gpt-")
    )


def _pinned_judge(root: Path) -> tuple[str, dict[str, Any], int]:
    """Read source-verified constants without importing tau2 or LiteLLM."""
    values: dict[str, Any] = {}

    def literal(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    raise ValueError("unpacked judge constants are unsupported")
                result[literal(key)] = literal(value)
            return result
        return ast.literal_eval(node)

    needed = {"DEFAULT_LLM_NL_ASSERTIONS", "DEFAULT_LLM_NL_ASSERTIONS_TEMPERATURE",
              "DEFAULT_LLM_NL_ASSERTIONS_ARGS", "DEFAULT_MAX_RETRIES"}
    for node in ast.parse((root / "src/tau2/config.py").read_text()).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in needed):
            values[node.targets[0].id] = literal(node.value)
    return (values["DEFAULT_LLM_NL_ASSERTIONS"], values["DEFAULT_LLM_NL_ASSERTIONS_ARGS"],
            values["DEFAULT_MAX_RETRIES"])


def _dependencies() -> dict[str, Any]:
    import json

    from safe_agent.tal.domain_runtime import (
        REVIEWED_TAL_BENCHMARK_COMMIT,
        require_reviewed_domain_runtime,
    )

    if sys.version_info[:2] != (3, 12):
        raise ValueError("BENCHMARK_PYTHON_PROFILE_MISMATCH")
    require_reviewed_domain_runtime()
    distribution = importlib.metadata.distribution("tau2")
    direct = json.loads(distribution.read_text("direct_url.json") or "null")
    if (distribution.version != TAU_RELEASE.removeprefix("v")
            or not isinstance(direct, dict)
            or direct.get("url") != "https://github.com/sierra-research/tau2-bench.git"
            or direct.get("vcs_info", {}).get("commit_id") != TAU_COMMIT
            or direct.get("vcs_info", {}).get("vcs") != "git"
            or direct.get("dir_info", {}).get("editable")):
        raise ValueError("BENCHMARK_DEPENDENCY_PROVENANCE_MISMATCH")
    return {"tal_commit": REVIEWED_TAL_BENCHMARK_COMMIT,
            "versions": {name: importlib.metadata.version(name) for name in
                         ("tervoro-safe-agent", "tau2", "pydantic", "litellm", "rank-bm25")}}


def _safe_revision() -> dict[str, Any]:
    from .runner import _repository_revision

    return _repository_revision()


def _output_available(path: Path) -> bool:
    target = path.absolute()
    if any(node.is_symlink() for node in (target, *target.parents)):
        return False
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return False
    parent = target if target.exists() else next(node for node in target.parents if node.exists())
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _probe_service(config: BenchmarkConfig) -> dict[str, Any]:
    from safe_agent.contracts.episode_inference import (
        EpisodeDispatchAuthenticator,
        EpisodeServiceCapabilities,
    )
    from safe_agent.inference.episode_gateway import EpisodeUnixTransport
    from safe_agent.inference_service.__main__ import _secret_from_file
    from safe_agent.inference_service.providers.openai_episode import openai_backend_capabilities

    socket_path = Path(os.environ["SAFE_EPISODE_SOCKET"])
    key_path = Path(os.environ["SAFE_EPISODE_DISPATCH_KEY_FILE"])
    # Keep the existing dispatch-key file contract. The shared loader checks
    # ownership, no-follow regular-file access, private mode, and finite size.
    if not key_path.is_absolute() or key_path.resolve() != key_path:
        raise ValueError("DISPATCH_KEY_PATH_DENIED")
    authenticator = EpisodeDispatchAuthenticator(_secret_from_file(key_path))
    response = EpisodeUnixTransport(socket_path, timeout_seconds=5).capabilities(
        authenticator,
        expected=EpisodeServiceCapabilities(
            backend=openai_backend_capabilities(model=config.llm_agent)
        ),
    )
    return {"verified": True, "configuration": response.reply.capabilities.model_dump(mode="json"),
            "proof_sha256": content_hash(response.model_dump(mode="json")),
            "provider_access_verified": False}


def preflight_run(config: BenchmarkConfig, *, tau_root: Path | None = None,
                  offline: bool = False) -> dict[str, Any]:
    """Inspect launch readiness; only online mode contacts a local Unix socket.

    ``ready_for_attempt`` means the declared launch requirements pass. The
    provider key is checked only for presence in the trusted harness process;
    its value is neither emitted nor sent anywhere by this function.
    """
    issues: list[str] = []
    warnings: list[str] = []
    report: dict[str, Any] = {
        "schema_version": "safe_agent.tau3_preflight.v1", "kind": "benchmark_preflight",
        "status": "blocked", "offline": offline, "model_calls": 0,
        "provider_requests": 0, "provider_access_verified": False,
        "config_sha256": None, "run_id": config.run_id,
        "configuration_validated": False, "ready_for_attempt": False,
        "issues": issues, "warnings": warnings,
        "sidecar": {"verified": False, "status": "not_checked"},
        "billing_scope": "SAFE reservation headroom only; total external invoices are not capped",
    }
    try:
        report["config_sha256"] = content_hash(config.model_dump())
    except (ValueError, TypeError):
        issues.append("CONFIGURATION_MUST_BE_FINITE_JSON")
        return report
    root = tau_root or (Path(config.tau_root) if config.tau_root else None)
    try:
        inventory = inspect_benchmark(config, root)
    except (ValueError, OSError, KeyError, TypeError):
        issues.append("BENCHMARK_SOURCE_OR_SELECTION_INVALID")
        return report
    report["inventory"] = inventory
    if root is None or inventory.get("status") != "ready":
        issues.append("FROZEN_BENCHMARK_SOURCE_UNAVAILABLE")
        return report
    if config.runtime_factory != CANONICAL_RUNTIME_FACTORY:
        issues.append("PREFLIGHT_REQUIRES_CANONICAL_PROFILE")
    try:
        report["dependencies"] = _dependencies()
    except Exception:
        issues.append("BENCHMARK_DEPENDENCIES_UNVERIFIED")
    try:
        revision = _safe_revision()
        report["safe_revision"] = revision
        if revision.get("dirty") is not False or not re.fullmatch(
            r"[0-9a-f]{40}", revision.get("commit") or ""
        ):
            if config.stage == "final":
                issues.append("FINAL_RUN_REQUIRES_CLEAN_RECORDED_REVISION")
            else:
                warnings.append("IMPLEMENTATION_REVISION_NOT_CLEAN")
    except Exception:
        issues.append("IMPLEMENTATION_REVISION_UNAVAILABLE")
    try:
        if not _output_available(Path(config.output_dir)):
            issues.append("OUTPUT_DIRECTORY_UNAVAILABLE")
    except OSError:
        issues.append("OUTPUT_DIRECTORY_UNAVAILABLE")

    selected = set(inventory["selected_task_ids"])
    if config.stage == "development" and not selected.issubset(config.development_task_ids):
        issues.append("PILOT_SELECTION_MUST_DECLARE_DEVELOPMENT_EXPOSURE")
    if len(config.development_task_ids) != len(set(config.development_task_ids)):
        issues.append("DUPLICATE_DEVELOPMENT_EXPOSURE")
    if config.stage == "final" and not config.development_task_ids:
        warnings.append("NO_DECLARED_DEVELOPMENT_EXPOSURE_REVIEW_REQUIRED")

    for model in (config.llm_agent, config.llm_user):
        if not _openai_model(model):
            issues.append("PREFLIGHT_SUPPORTS_EXPLICIT_OPENAI_MODEL_NAMES_ONLY")
            break
    try:
        _harness_arguments(config.llm_args_user)
        if config.system == "llm_agent":
            _harness_arguments(config.llm_args_agent)
            report["safe_budget_applies"] = False
        else:
            budgets = _runtime_configuration(config)
            report["effective_safe_limits"] = budgets
            report["safe_budget_applies"] = True
            episode, run = budgets["episode_budget"], budgets["run_budget"]
            minimum_token_hold = budgets["minimum_token_hold"]
            conversations = inventory["scheduled_conversations"]
            hold = episode["max_attempt_cost_microusd"]
            if (episode["max_tokens"] < minimum_token_hold
                    or (hold and episode["max_cost_microusd"] < hold)):
                issues.append("EPISODE_CANNOT_RESERVE_A_MODEL_ATTEMPT")
            if (run["max_turns"] < conversations
                    or run["max_model_calls"] < conversations
                    or run["max_tokens"] < minimum_token_hold * conversations
                    or (hold and run["max_cost_microusd"] < hold * conversations)):
                issues.append("RUN_CANNOT_RESERVE_ONE_ATTEMPT_PER_CONVERSATION")
            if not hold:
                warnings.append("ZERO_COST_HOLD_IS_NOT_FREE_PROVIDER_INFERENCE")
            effective = config.model_copy(update={"episode_budget": episode, "run_budget": run})
            report["safe_reservation_headroom"] = reservation_headroom(effective, conversations)
    except (ValueError, TypeError):
        issues.append("RUNTIME_LIMITS_OR_MODEL_ARGUMENTS_INVALID")

    try:
        judge, judge_args, default_retries = _pinned_judge(root)
        roles: dict[str, dict[str, Any]] = {
            "agent": {"model": config.llm_agent, "provider": "openai",
                      "provider_num_retries": 0 if config.system == "safe_agent" else
                      _reported_retries(config.llm_args_agent, default_retries)},
            "user": {"model": config.llm_user, "provider": "openai",
                     "provider_num_retries": _reported_retries(config.llm_args_user, default_retries)},
        }
        if inventory["selected_tasks_with_nl_assertions"]:
            roles["judge"] = {"model": judge, "provider": "openai",
                              "provider_num_retries": default_retries, "model_args": judge_args}
        report["model_roles"] = roles
        report["simulation_max_retries"] = 0
        report["selected_tasks_requiring_hosted_judge"] = inventory["selected_tasks_with_nl_assertions"]
        report["provider_retry_note"] = (
            "Upstream num_retries is separate from simulation retries; it is not a measured call count"
        )
    except (ValueError, OSError, KeyError, TypeError):
        issues.append("PINNED_JUDGE_CONFIGURATION_UNAVAILABLE")

    report["configuration_validated"] = not issues
    if offline:
        report["status"] = "configuration_validated" if not issues else "blocked"
        report["harness_credential_presence"] = "not_checked"
        return report

    # Do not import LiteLLM: import itself can load .env or download a cost map.
    # Require an explicit reproducible process environment before the runner
    # later imports the frozen official harness.
    if os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True":
        issues.append("SET_LITELLM_LOCAL_MODEL_COST_MAP_TRUE")
    if os.environ.get("LITELLM_MODE") != "PRODUCTION":
        issues.append("SET_LITELLM_MODE_PRODUCTION_TO_DISABLE_IMPLICIT_DOTENV")
    if any(os.environ.get(name) for name in
           ("OPENAI_BASE_URL", "OPENAI_API_BASE", "LITELLM_PROXY_API_BASE", "LITELLM_API_BASE")):
        issues.append("PROVIDER_ENDPOINT_OVERRIDE_UNSUPPORTED")
    credential_present = bool(os.environ.get("OPENAI_API_KEY"))
    report["harness_credential_presence"] = credential_present
    if not credential_present:
        issues.append("HARNESS_OPENAI_CREDENTIAL_NOT_CONFIGURED")
    if config.system == "safe_agent":
        if not os.environ.get("SAFE_EPISODE_SOCKET") or not os.environ.get("SAFE_EPISODE_DISPATCH_KEY_FILE"):
            issues.append("SIDECAR_SOCKET_AND_DISPATCH_KEY_NOT_CONFIGURED")
        elif report["configuration_validated"]:
            try:
                report["sidecar"] = _probe_service(config)
            except Exception:
                issues.append("SIDECAR_CAPABILITY_VERIFICATION_FAILED")
    else:
        report["sidecar"] = {"verified": False, "status": "not_used_by_stock_agent"}
    report["status"] = "ready_for_attempt" if not issues else "blocked"
    report["ready_for_attempt"] = not issues
    return report
