"""Explicit run configuration, with no provider calls at load time."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from safe_agent.contracts.episode_inference import (
    EpisodeProposalFormat,
    LocalTimingProfile,
    validate_local_api_base,
)

from . import TAU_COMMIT
from .protocol import BoundaryModel

CANONICAL_RUNTIME_FACTORY = "safe_agent.orchestration.episode_runtime:runtime_factory"


def _model_defaults() -> dict[str, JsonValue]:
    return {"temperature": 0.0}


class BenchmarkConfig(BoundaryModel):
    schema_version: Literal[1] = 1
    tau_commit: Literal["fc0055dc4e0a316c3f83133267fbd6faaa770992"] = TAU_COMMIT
    tau_root: str | None = None
    domain: Literal["retail", "banking_knowledge"] = "banking_knowledge"
    system: Literal["safe_agent", "llm_agent"] = "safe_agent"
    stage: Literal["development", "coverage", "final"] = "development"
    run_id: str = Field(default="tau3-development", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    output_dir: str = "artifacts/tau3/development"
    task_split_name: Literal["base", "train", "test"] = "base"
    task_ids: list[str] | None = None
    num_tasks: int | None = Field(default=None, gt=0)
    num_trials: int = Field(default=1, gt=0)
    seed: int = 300
    max_steps: int = Field(default=200, gt=0)
    max_errors: int = Field(default=10, gt=0)
    max_concurrency: Literal[1] = 1
    timeout: float | None = Field(default=None, gt=0)
    max_retries: Literal[0] = 0
    retrieval_config: Literal["bm25"] = "bm25"
    retrieval_config_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    model_route: Literal["openai", "local"] = "openai"
    local_api_base: str | None = None
    local_timing: LocalTimingProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_proposal_format: EpisodeProposalFormat = Field(
        default="candidate_json_v1", exclude_if=lambda value: value == "candidate_json_v1",
    )
    stop_after_infrastructure_errors: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None,
    )
    llm_agent: str = "gpt-4.1-2025-04-14"
    llm_args_agent: dict[str, JsonValue] = Field(default_factory=_model_defaults)
    llm_user: str = "gpt-4.1-2025-04-14"
    llm_args_user: dict[str, JsonValue] = Field(default_factory=_model_defaults)
    development_task_ids: list[str] = Field(default_factory=list)
    runtime_factory: str = CANONICAL_RUNTIME_FACTORY
    episode_budget: dict[str, JsonValue] = Field(default_factory=dict)
    run_budget: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.stop_after_infrastructure_errors is not None and self.stage != "development":
            raise ValueError("INFRASTRUCTURE_STOP_LIMIT_REQUIRES_DEVELOPMENT_STAGE")
        if self.task_ids is not None:
            if not self.task_ids or len(self.task_ids) != len(set(self.task_ids)):
                raise ValueError("task_ids must be a nonempty set of unique IDs")
        if self.domain == "banking_knowledge" and self.task_split_name != "base":
            raise ValueError("Pinned banking has no implemented train/test split")
        if self.stage in {"coverage", "final"}:
            if self.task_ids is not None or self.num_tasks is not None:
                raise ValueError("Coverage/final runs must include the complete selected domain")
            if self.task_split_name != "base":
                raise ValueError("Coverage/final runs use the full base set")
        if self.stage == "final" and self.num_trials < 4:
            raise ValueError("The frozen comparison requires at least four trials")
        if self.stage != "development" and self.runtime_factory != CANONICAL_RUNTIME_FACTORY:
            raise ValueError("Coverage/final runs require the canonical SAFE runtime factory")
        if not self.llm_agent.strip() or not self.llm_user.strip():
            raise ValueError("Exact agent and user model identifiers are required")
        if self.model_route == "local":
            if self.local_api_base is None:
                raise ValueError("The local route requires an explicit local_api_base")
            validate_local_api_base(self.local_api_base)
            if self.stage != "development":
                raise ValueError("The local route currently supports development pilots only")
        elif self.local_api_base is not None or self.local_timing is not None:
            raise ValueError("The hosted route does not accept a local_api_base")
        if self.agent_proposal_format != "candidate_json_v1" and (
            self.model_route != "local" or self.system != "safe_agent"
            or self.runtime_factory != CANONICAL_RUNTIME_FACTORY
        ):
            raise ValueError("NATIVE_PROPOSALS_REQUIRE_CANONICAL_LOCAL_SAFE_ROUTE")
        return self


def harness_model_settings(
    config: BenchmarkConfig, role: Literal["agent", "user"]
) -> tuple[str, dict[str, JsonValue]]:
    """Resolve a declared route without secrets, imports, or provider calls.

    SAFE's agent keeps the raw model alias because its Unix sidecar selects the
    backend. The stock agent and simulated user use the pinned LiteLLM harness.
    Endpoint and dummy-key arguments are inserted here, never accepted from
    untrusted model arguments or forwarded to SAFE's planner.
    """
    model = config.llm_agent if role == "agent" else config.llm_user
    arguments = dict(config.llm_args_agent if role == "agent" else config.llm_args_user)
    if config.model_route == "local" and (role == "user" or config.system == "llm_agent"):
        if config.local_api_base is None:
            raise ValueError("LOCAL_API_BASE_REQUIRED")
        arguments.update(
            api_base=validate_local_api_base(config.local_api_base),
            api_key="safe-local-no-key",
            num_retries=0,
        )
        if config.local_timing is not None:
            arguments["timeout"] = config.local_timing.provider_timeout_seconds
        model = "openai/" + model
    return model, arguments


def effective_inference_timing(config: BenchmarkConfig) -> dict[str, JsonValue]:
    """Declared effective timing, without inference or guessed SDK defaults."""
    profile = config.local_timing
    if profile is not None:
        profile = LocalTimingProfile.model_validate(profile.model_dump())
        if config.model_route != "local" or config.stage != "development":
            raise ValueError("LOCAL_TIMING_REQUIRES_LOCAL_DEVELOPMENT_ROUTE")
    return {
        "basis": "explicit_local_profile" if profile is not None else "legacy_fixed_defaults",
        "profile": profile.model_dump(mode="json") if profile is not None else None,
        "provider_timeout_seconds": profile.provider_timeout_seconds if profile is not None else 60.0,
        "transport_timeout_seconds": profile.transport_timeout_seconds if profile is not None else 60.0,
        "request_timeout_seconds": profile.request_timeout_seconds if profile is not None else 90.0,
        "harness_provider_timeout_seconds": profile.provider_timeout_seconds if profile is not None else None,
        "max_inference_seconds": 120,
        "absolute_local_deadline_enforced": profile is not None,
        "simulation_timeout_seconds": config.timeout,
    }


def load_config(path: str | Path) -> BenchmarkConfig:
    with Path(path).open("rb") as stream:
        return BenchmarkConfig.model_validate(tomllib.load(stream))
