"""Strict proposals and trusted observation state for benchmark domain packs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from safe_agent.contracts.base import Digest, Identifier, StrictModel
from safe_agent.contracts.episodes import (
    EpisodeDomain,
    OperationName,
    arguments_digest,
    canonical_arguments_json,
    effect_digest,
)

DomainEffect = Literal["read", "write", "control"]


class ToolProposal(StrictModel):
    """Model-proposed arguments; the catalog, not the model, determines effects."""

    domain: EpisodeDomain
    tool_name: OperationName
    arguments: dict[str, JsonValue]

    @model_validator(mode="after")
    def native_json(self) -> ToolProposal:
        canonical_arguments_json(self.arguments)
        return self

    @property
    def arguments_json(self) -> str:
        return canonical_arguments_json(self.arguments)

    @property
    def arguments_hash(self) -> str:
        return arguments_digest(self.arguments_json)

    @property
    def digest(self) -> str:
        return effect_digest(self.tool_name, self.arguments_hash)


class ToolObservation(StrictModel):
    """Produced from a correlated official tool response, never a model claim."""

    tool_name: OperationName
    arguments: dict[str, JsonValue]
    result: JsonValue
    success: bool = True


class CustomerConsent(StrictModel):
    """Trusted, action-bound customer consent; not an office operator approval."""

    episode_id: Identifier
    proposal_digest: Digest
    subject_id: str = Field(min_length=1, max_length=256)
    user_turn_id: Identifier
    policy_hash: Digest


class DomainContext(StrictModel):
    """Trusted-host state only. Do not include this contract in model output schemas.

    Subject, authentication, discovery and entity records must come from the
    pack's observation methods or an equivalent protected host integration.
    User messages are actual inbound turns, not model-created verification text.
    """

    episode_id: Identifier
    domain: EpisodeDomain
    policy_hash: Digest
    observation_hash: Digest
    state_version: int = Field(ge=0)
    subject_id: str | None = None
    authenticated: bool = False
    observations: tuple[ToolObservation, ...] = ()
    consent: CustomerConsent | None = None
    discovered_tools: tuple[str, ...] = ()
    unlocked_tools: tuple[str, ...] = ()
    user_provided_identity_values: tuple[str, ...] = ()
    user_messages: tuple[str, ...] = ()
    latest_user_turn_id: Identifier | None = None
    entities: dict[str, JsonValue] = Field(default_factory=dict)
    entity_owners: dict[str, str] = Field(default_factory=dict)
    unresolved_write: bool = False
    tool_calls_used: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=1000, ge=1, le=1000000)


class DomainDecision(StrictModel):
    allowed: bool
    reason_code: str
    effect: DomainEffect
    # Dispatch/retry effects can include wrapper logging. Completion receipts
    # must use the underlying operation's business effect instead.
    business_effect: DomainEffect
    proposal_digest: Digest
    effective_tool_name: str
    requires_customer_confirmation: bool = False
    tal_decision_hash: Digest | None = None
    authorizes_execution: Literal[False] = False
    semantic_policy_verified: Literal[False] = False


class Capability(StrictModel):
    """Public signature metadata pinned from upstream, not task/evaluator data."""

    name: OperationName
    parameters: dict[str, JsonValue]
    effect: DomainEffect
    mutates_state: bool
    discoverable: bool = False
    actor: Literal["agent", "user"] = "agent"
    requires_authentication: bool = False
    requires_subject: bool = False
    requires_customer_confirmation: bool = False
