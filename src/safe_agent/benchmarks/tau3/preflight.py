"""No-inference launch checks for hosted and local benchmark conditions.

An authenticated local capability exchange proves a matching sidecar
configuration. It does not test provider credentials, quota, or model access.
Local mode can also inspect the server's model listing, without generation.
Nothing here constructs an official agent, opens a run database, or reserves
inference budget. Offline mode does not read dispatch keys or contact endpoints.
"""

from __future__ import annotations

import ast
import importlib.metadata
import math
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from safe_agent.contracts.episode_inference import (
    EpisodeInferenceRequest,
    strict_json,
    validate_episode_model_arguments,
    validate_local_api_base,
)

from . import TAU_COMMIT, TAU_RELEASE
from .config import CANONICAL_RUNTIME_FACTORY, BenchmarkConfig, effective_inference_timing
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
    if config.local_timing is not None and (
        min(episode.deadline_seconds, run.deadline_seconds) < config.local_timing.request_timeout_seconds
    ):
        raise ValueError("EPISODE_CANNOT_FIT_LOCAL_REQUEST_LIFETIME")
    validate_episode_model_arguments(
        config.llm_args_agent, max_output_tokens=episode.max_output_tokens
    )
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


def _harness_arguments(arguments: dict[str, Any], *, local: bool = False) -> None:
    # Routing comes from typed top-level configuration, never hidden
    # credentials, endpoint overrides, callbacks, or mock replies in arguments.
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
    if local and "num_retries" in arguments and (type(retries) is not int or retries != 0):
        raise ValueError("LOCAL_HARNESS_RETRIES_MUST_BE_ZERO")
    tokens = arguments.get("max_tokens")
    if tokens is not None and (type(tokens) is not int or tokens <= 0):
        raise ValueError("INVALID_HARNESS_MODEL_ARGUMENTS")


def _reported_retries(arguments: dict[str, Any], default: int) -> int | None:
    value = arguments.get("num_retries")
    if value is None:
        return default
    # Invalid model arguments block launch but must not echo arbitrary values.
    return value if type(value) is int and 0 <= value <= 20 else None


def _reported_output_cap(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("max_tokens")
    # None means no explicit cap was supplied, not an unlimited provider.
    return value if type(value) is int and value > 0 else None


def _openai_model(model: str) -> bool:
    # Routing syntax only. A name passing this check may still be unavailable
    # to the account; preflight never contacts the provider to find out.
    return bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", model)) and model.startswith(
        ("gpt-", "chatgpt-", "o1", "o3", "o4", "ft:gpt-")
    )


def _local_model(model: str) -> bool:
    # Alias grammar also fits the signed sidecar protocol. Explicit cloud tags
    # are rejected, but an alias cannot attest what a local server executes.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model)) and not model.lower().endswith(
        (":cloud", "-cloud")
    )


def _probe_local_models(config: BenchmarkConfig, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Bounded local metadata request; no key lookup, model load, or generation."""
    import httpx

    from safe_agent.inference.deadline_http import DeadlineBudget, DeadlineHTTPTransport

    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5):
        raise ValueError("LOCAL_METADATA_TIMEOUT_INVALID")
    if config.local_api_base is None:
        raise ValueError("LOCAL_API_BASE_REQUIRED")
    base = validate_local_api_base(config.local_api_base)
    payload = bytearray()
    transport = DeadlineHTTPTransport(DeadlineBudget(time.monotonic() + timeout_seconds))
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                      timeout=timeout_seconds) as client:
        with client.stream("GET", base + "/models", headers={
            "Authorization": "Bearer safe-local-no-key", "Accept-Encoding": "identity",
        }) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("LOCAL_MODEL_LIST_ENCODING_DENIED")
            for chunk in response.iter_raw():
                payload.extend(chunk)
                if len(payload) > 65_536:
                    raise ValueError("LOCAL_MODEL_LIST_TOO_LARGE")
    document = strict_json(payload.decode("utf-8"), max_bytes=65_536)
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValueError("LOCAL_MODEL_LIST_INVALID")
    available = {row["id"] for row in document["data"]
                 if isinstance(row, dict) and isinstance(row.get("id"), str)}
    required = {config.llm_agent, config.llm_user}
    if not required.issubset(available):
        raise ValueError("LOCAL_REQUIRED_MODELS_UNAVAILABLE")
    return {"verified": True, "status": "selected_models_listed", "api_base": base,
            "selected_models": sorted(required), "inference_verified": False,
            "local_execution_attested": False, "model_weights_attested": False}


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
    from safe_agent.inference_service.providers.local_openai_episode import (
        local_openai_backend_capabilities,
    )
    from safe_agent.inference_service.providers.openai_episode import openai_backend_capabilities

    socket_path = Path(os.environ["SAFE_EPISODE_SOCKET"])
    key_path = Path(os.environ["SAFE_EPISODE_DISPATCH_KEY_FILE"])
    # Keep the existing dispatch-key file contract. The shared loader checks
    # ownership, no-follow regular-file access, private mode, and finite size.
    if not key_path.is_absolute() or key_path.resolve() != key_path:
        raise ValueError("DISPATCH_KEY_PATH_DENIED")
    authenticator = EpisodeDispatchAuthenticator(_secret_from_file(key_path))
    backend = (
        local_openai_backend_capabilities(model=config.llm_agent, base_url=config.local_api_base,
                                          local_timing=config.local_timing,
                                          proposal_format=config.agent_proposal_format)
        if config.model_route == "local" and config.local_api_base is not None
        else openai_backend_capabilities(model=config.llm_agent)
    )
    response = EpisodeUnixTransport(socket_path, timeout_seconds=5).capabilities(
        authenticator,
        expected=EpisodeServiceCapabilities(backend=backend),
        absolute_timeout_seconds=5.0 if config.model_route == "local" else None,
    )
    return {"verified": True, "configuration": response.reply.capabilities.model_dump(mode="json"),
            "proof_sha256": content_hash(response.model_dump(mode="json")),
            "provider_access_verified": False}


def preflight_run(config: BenchmarkConfig, *, tau_root: Path | None = None,
                  offline: bool = False) -> dict[str, Any]:
    """Inspect launch readiness; online mode contacts declared local endpoints.

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
        "inference_requests": 0, "model_server_metadata_requests": 0,
        "model_route": config.model_route,
        "config_sha256": None, "run_id": config.run_id,
        "configuration_validated": False, "ready_for_attempt": False,
        "issues": issues, "warnings": warnings,
        "sidecar": {"verified": False, "status": "not_checked"},
        "local_server": {"verified": False, "status": "not_checked"},
        "billing_scope": "SAFE reservation headroom only; total external invoices are not capped",
    }
    try:
        report["config_sha256"] = content_hash(config.model_dump())
    except (ValueError, TypeError):
        issues.append("CONFIGURATION_MUST_BE_FINITE_JSON")
        return report
    try:
        report["effective_inference_timing"] = effective_inference_timing(config)
    except (ValueError, TypeError, AttributeError):
        issues.append("LOCAL_TIMING_PROFILE_INVALID")
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

    local = config.model_route == "local"
    if local:
        try:
            if config.local_api_base is None:
                raise ValueError("LOCAL_API_BASE_REQUIRED")
            validate_local_api_base(config.local_api_base)
        except (ValueError, TypeError):
            issues.append("LOCAL_API_BASE_INVALID")
        if config.stage != "development" or inventory["selected_tasks_with_nl_assertions"]:
            issues.append("LOCAL_ROUTE_REQUIRES_DEVELOPMENT_TASKS_WITHOUT_HOSTED_JUDGE")
        if not all(_local_model(model) for model in (config.llm_agent, config.llm_user)):
            issues.append("LOCAL_MODEL_ALIASES_INVALID_OR_CLOUD_TAGGED")
        warnings.append("LOCAL_SERVER_EXECUTION_AND_MODEL_WEIGHTS_NOT_ATTESTED")
        report["billing_scope"] = (
            "Local endpoint condition; reservation holds and upstream zero prices do not "
            "measure hardware, electricity, or server operating costs"
        )
    else:
        if config.model_route != "openai" or config.local_api_base is not None:
            issues.append("MODEL_ROUTE_CONFIGURATION_INVALID")
        if not all(_openai_model(model) for model in (config.llm_agent, config.llm_user)):
            issues.append("PREFLIGHT_SUPPORTS_EXPLICIT_OPENAI_MODEL_NAMES_ONLY")
    try:
        _harness_arguments(config.llm_args_user, local=local)
        if config.system == "llm_agent":
            _harness_arguments(config.llm_args_agent, local=local)
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
            if not hold and not local:
                warnings.append("ZERO_COST_HOLD_IS_NOT_FREE_PROVIDER_INFERENCE")
            effective = config.model_copy(update={"episode_budget": episode, "run_budget": run})
            report["safe_reservation_headroom"] = reservation_headroom(effective, conversations)
    except (ValueError, TypeError):
        issues.append("RUNTIME_LIMITS_OR_MODEL_ARGUMENTS_INVALID")

    try:
        judge, judge_args, default_retries = _pinned_judge(root)
        provider = "local_openai_compatible" if local else "openai"
        harness_retries = 0 if local else default_retries
        roles: dict[str, dict[str, Any]] = {
            "agent": {"model": config.llm_agent, "provider": provider,
                      "provider_num_retries": 0 if config.system == "safe_agent" else
                      _reported_retries(config.llm_args_agent, harness_retries),
                      "max_output_tokens": (
                          report.get("effective_safe_limits", {}).get("episode_budget", {}).get(
                              "max_output_tokens"
                          ) if config.system == "safe_agent" else
                          _reported_output_cap(config.llm_args_agent)
                      ),
                      "output_cap_basis": "safe_runtime_budget" if config.system == "safe_agent"
                      else "explicit_harness_argument_or_provider_default"},
            "user": {"model": config.llm_user, "provider": provider,
                     "provider_num_retries": _reported_retries(config.llm_args_user, harness_retries),
                     "max_output_tokens": _reported_output_cap(config.llm_args_user),
                     "output_cap_basis": "explicit_harness_argument_or_provider_default"},
        }
        for role, settings in roles.items():
            settings["provider_timeout_seconds"] = (
                config.local_timing.provider_timeout_seconds if config.local_timing is not None
                else 60.0 if role == "agent" and config.system == "safe_agent" else None
            )
            settings["absolute_local_deadline_enforced"] = config.local_timing is not None
        if inventory["selected_tasks_with_nl_assertions"]:
            roles["judge"] = {"model": judge, "provider": "openai",
                              "provider_num_retries": default_retries, "model_args": judge_args}
        report["model_roles"] = roles
        report["simulation_max_retries"] = 0
        report["selected_tasks_requiring_hosted_judge"] = inventory["selected_tasks_with_nl_assertions"]
        report["provider_retry_note"] = (
            "Upstream num_retries is separate from simulation retries; it is not a measured call count"
        )
        report["output_cap_note"] = (
            "A null max_output_tokens means no explicit validated cap is reported; "
            "it does not establish the provider default or equal output limits"
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
    if local:
        report["harness_credential_presence"] = "not_required_for_local_route"
        if report["configuration_validated"]:
            report["model_server_metadata_requests"] = 1
            report["provider_requests"] = 1
            try:
                report["local_server"] = _probe_local_models(config)
            except Exception:
                issues.append("LOCAL_MODEL_SERVER_UNAVAILABLE_OR_MODELS_MISSING")
    else:
        report["local_server"] = {"verified": False, "status": "not_used_by_hosted_route"}
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
