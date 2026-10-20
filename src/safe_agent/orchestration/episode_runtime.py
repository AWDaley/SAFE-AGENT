"""Trusted multi-turn runtime used by the official τ text adapter.

Only observations cross in and one message or durably authorized tool call
crosses out. This module never receives a Task, an evaluator, a DB snapshot, or
an executable benchmark tool. The official orchestrator alone executes tools.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import Field, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.schema import Table

from safe_agent.benchmarks.tau3.protocol import (
    Decision,
    Message,
    Observation,
    RuntimeContext,
    ToolSpec,
)
from safe_agent.canonical import sha256
from safe_agent.contracts.base import StrictModel
from safe_agent.contracts.episode_inference import (
    EpisodeDispatchAuthenticator,
    EpisodeInferenceRequest,
    MessageCandidate,
    ToolCandidate,
    strict_json,
    wire_digest,
    wire_json,
)
from safe_agent.contracts.episodes import (
    AuthorizationBinding,
    EpisodeLimits,
    EpisodeSpec,
    EpisodeState,
    EpisodeView,
    OutgoingCall,
    RunSpec,
)
from safe_agent.conversation.consent import (
    classify_consent,
    confirmation_text,
    consent_evidence,
)
from safe_agent.conversation.context_evidence import (
    CONTEXT_RETENTION_PROFILE,
    ContextRetentionDenied,
    ObservedResult,
    collect_evidence,
    fit_optional,
)
from safe_agent.conversation.intake import ProjectionDenied, ReferenceVault, identity_values
from safe_agent.conversation.renderer import ResponseDenied, render_response
from safe_agent.domains import get_domain_pack
from safe_agent.domains.contracts import CustomerConsent, DomainContext, ToolProposal
from safe_agent.inference.episode_gateway import (
    EpisodeInferenceDenied,
    EpisodeInferenceGateway,
    EpisodeTransport,
    EpisodeUnixTransport,
)
from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
    RuntimeStorageDenied,
)
from safe_agent.storage.episodes import EpisodeStore
from safe_agent.storage.models import Base


class RuntimeLimits(StrictModel):
    max_turns: int = Field(default=200, ge=1)
    max_model_calls: int = Field(default=200, ge=1)
    max_tool_calls: int = Field(default=200, ge=1)
    max_tokens: int = Field(default=2_000_000, ge=1)
    max_cost_microusd: int = Field(default=10_000_000, ge=0)
    deadline_seconds: int = Field(default=1800, ge=1, le=604800)
    max_prompt_bytes: int = Field(default=131_072, ge=8192, le=196_608)
    max_output_tokens: int = Field(default=2048, ge=64, le=16_384)
    max_attempt_cost_microusd: int = Field(default=100_000, ge=0)
    max_schema_repairs: int = Field(default=1, ge=0, le=2)

    def episode_limits(self) -> EpisodeLimits:
        return EpisodeLimits(**{name: getattr(self, name) for name in EpisodeLimits.model_fields})


class RunLimits(StrictModel):
    max_turns: int = Field(default=200_000, ge=1)
    max_model_calls: int = Field(default=200_000, ge=1)
    max_tool_calls: int = Field(default=200_000, ge=1)
    max_tokens: int = Field(default=2_000_000_000, ge=1)
    max_cost_microusd: int = Field(default=1_000_000_000, ge=0)
    deadline_seconds: int = Field(default=86400, ge=1, le=604800)

    def episode_limits(self) -> EpisodeLimits:
        return EpisodeLimits(**{name: getattr(self, name) for name in EpisodeLimits.model_fields})


class EpisodeRuntime:
    """One configured run; private state and aggregate caps survive new agents."""

    def __init__(self, context: RuntimeContext, *, engine: Engine,
                 transport: EpisodeTransport,
                 authenticator: EpisodeDispatchAuthenticator | None = None,
                 admission_evaluator: Any = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        # Frozen Pydantic records may still contain caller-owned mutable dicts.
        context = RuntimeContext.model_validate_json(context.model_dump_json())
        self.context = context
        self.clock = clock
        self.limits = RuntimeLimits.model_validate(context.episode_budget)
        self.run_limits = RunLimits.model_validate(context.run_budget)
        self.episodes = EpisodeStore(engine)
        self.private = EpisodeRuntimeStore(engine)
        self.gateway = EpisodeInferenceGateway(
            episodes=self.episodes, journal=self.private, transport=transport,
            authenticator=authenticator or EpisodeDispatchAuthenticator(SecretStr(uuid4().hex)),
            clock=clock,
        )
        self._seed: int | None = None
        self._admission_evaluator = admission_evaluator
        if set(context.model_args) - {"temperature", "seed"}:
            raise ValueError("UNSUPPORTED_EPISODE_MODEL_ARGUMENTS")
        if not context.run_id:
            raise ValueError("INVALID_EPISODE_RUN_ID")
        self.run_id = context.run_id if context.run_id[0].isalpha() else "run_" + context.run_id
        config_hash = wire_digest({"profile": "tau3-v1.0.1-episode-v1",
                                   "context_retention_profile": CONTEXT_RETENTION_PROFILE,
                                   "domain": context.domain, "model": context.model,
                                   "model_args": context.model_args,
                                   "episode_limits": self.limits.model_dump(),
                                   "run_limits": self.run_limits.model_dump()})
        now = clock()
        try:
            existing = self.episodes.get_run(self.run_id)
        except KeyError:
            existing = self.episodes.create_run(RunSpec(
                run_id=self.run_id, organization_id="benchmark",
                policy_hash=config_hash, limits=self.run_limits.episode_limits(),
                created_at=now, deadline=now + timedelta(seconds=self.run_limits.deadline_seconds),
            ))
        if existing.spec.policy_hash != config_hash:
            raise RuntimeStorageDenied("RUN_CONFIGURATION_CHANGED")
        self.run_spec = existing.spec

    def set_seed(self, seed: int) -> None:
        self._seed = seed

    @staticmethod
    def create_fixture_schema(engine: Engine) -> None:
        """Isolated benchmark SQLite only; never creates office application tables."""
        EpisodeStore(engine).create_schema()
        Base.metadata.create_all(engine, tables=[cast(Table, EpisodePrivateContextRow.__table__),
                                                 cast(Table, EpisodeModelAttemptRow.__table__)])

    def _pack(self, data: dict[str, Any]) -> Any:
        kwargs = {}
        if self._admission_evaluator is not None:
            kwargs["admission_evaluator"] = self._admission_evaluator
        return get_domain_pack(data["domain"],
                               tool_schemas=[ToolSpec.model_validate(tool) for tool in data["tools"]],
                               **kwargs)

    @staticmethod
    def _domain_context(data: dict[str, Any]) -> DomainContext:
        return DomainContext.model_validate_json(wire_json(data["domain_context"]))

    @staticmethod
    def _policy_hash(domain: str, policy: str, tools: list[dict[str, Any]]) -> str:
        return wire_digest({"domain": domain, "policy": policy, "tools": tools,
                            "admission_profile": "tau3-v1.0.1-encoded-rules-v1",
                            "context_retention_profile": CONTEXT_RETENTION_PROFILE})

    def _owned_episode(self, episode_id: str) -> EpisodeView:
        view = self.episodes.inspect(episode_id)
        if view.run_id != self.run_id or view.domain != self.context.domain:
            raise RuntimeStorageDenied("EPISODE_RUN_SCOPE_DENIED")
        return view

    def _validate_sources(self, data: dict[str, Any], view: EpisodeView) -> None:
        """Private mutable context cannot replace the frozen episode sources."""
        try:
            ctx = self._domain_context(data)
            valid = (
                data["episode_id"] == ctx.episode_id == view.episode_id
                and data["domain"] == ctx.domain == view.domain
                and data["policy_hash"] == ctx.policy_hash == view.policy_hash
                and self._policy_hash(data["domain"], data["policy"], data["tools"])
                == view.policy_hash
                and datetime.fromisoformat(data["deadline"]) == view.deadline
                and ctx.observation_hash == view.observation_hash
                and ctx.max_tool_calls == view.limits.max_tool_calls
                and view.limits == self.limits.episode_limits()
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeStorageDenied("EPISODE_SOURCE_BINDING_CHANGED")

    def _vault(self, data: dict[str, Any]) -> ReferenceVault:
        ctx = self._domain_context(data)
        return ReferenceVault(episode_id=data["episode_id"], actor_id="benchmark_customer",
                              deadline=datetime.fromisoformat(data["deadline"]),
                              tools=data["tools"], records=data["private_references"],
                              customer_texts=ctx.user_messages,
                              resource_subjects=ctx.entity_owners)

    def start_episode(self, *, episode_id: str, domain: str, policy: str,
                      tools: list[ToolSpec], history: list[Message]) -> dict[str, Any]:
        if domain != self.context.domain:
            raise ValueError("DOMAIN_CONFIGURATION_MISMATCH")
        domain = self.context.domain
        now = self.clock()
        policy_hash = self._policy_hash(domain, policy, [tool.model_dump() for tool in tools])
        deadline = min(now + timedelta(seconds=self.limits.deadline_seconds), self.run_spec.deadline)
        spec = EpisodeSpec(episode_id=episode_id, run_id=self.run_id,
                           actor_id="benchmark_customer", domain=self.context.domain, policy_hash=policy_hash,
                           limits=self.limits.episode_limits(), created_at=now, deadline=deadline)
        view = self.episodes.create_episode(spec)
        ctx = DomainContext(episode_id=episode_id, domain=self.context.domain, policy_hash=policy_hash,
                            observation_hash=view.observation_hash, state_version=view.state_version,
                            max_tool_calls=self.limits.max_tool_calls)
        data: dict[str, Any] = {
            "episode_id": episode_id, "domain": domain, "policy": policy,
            "policy_hash": policy_hash, "tools": [tool.model_dump() for tool in tools],
            "deadline": deadline.isoformat(), "domain_context": ctx.model_dump(mode="json"),
            "history": [], "private_references": [], "seen_observations": [],
            "pending_consent": None, "pending_call": None,
            "successful_calls": {}, "diagnostics": [], "model_usage": [],
        }
        pack = self._pack(data)
        prior_calls: dict[str, ToolProposal] = {}
        for index, message in enumerate(history):
            if message.role == "user":
                ident = "history_" + str(index)
                ctx = pack.observe_user(message.content or "", ctx, user_turn_id=ident,
                                        identity_values=identity_values(message.content or ""))
            elif message.role == "assistant" and message.call_id and message.tool_name:
                prior_calls[message.call_id] = ToolProposal(
                    domain=self.context.domain, tool_name=message.tool_name, arguments=message.arguments or {})
            elif message.role == "tool":
                proposal = prior_calls.pop(message.call_id or "", None)
                if proposal is None:
                    raise RuntimeStorageDenied("UNMATCHED_HISTORY_RESULT")
                result, success = self._parse_result(message)
                ctx = pack.observe(proposal, result, ctx, success=success)
            data["history"].append(message.model_dump(mode="json"))
        if prior_calls:
            raise RuntimeStorageDenied("UNRESOLVED_PREEXISTING_HISTORY")
        # Pre-existing history is observation evidence, never retroactive SAFE
        # authorization or a new executable call. It has no success receipts.
        data["domain_context"] = ctx.model_dump(mode="json")
        self.private.create(episode_id, data)
        return {"episode_id": episode_id, "version": 0}

    @staticmethod
    def _parse_result(message: Message) -> tuple[Any, bool]:
        raw = message.content or ""
        try:
            value = strict_json(raw, max_bytes=4_194_304)
        except (ValueError, TypeError):
            value = raw
        success = (not message.error and not (isinstance(value, dict) and "error" in value)
                   and not (isinstance(value, str)
                            and re.match(r"\s*(error\b|failed\b)", value, re.IGNORECASE)))
        return value, success

    def _observe(self, observation: Observation, data: dict[str, Any]) -> DomainContext:
        episode_id = data["episode_id"]
        ctx = self._domain_context(data)
        pack = self._pack(data)
        for message in observation.messages:
            if message.role == "user":
                if data["pending_call"] is not None:
                    raise RuntimeStorageDenied("CUSTOMER_TURN_BEFORE_TOOL_RESULT")
                message_id = "msg_" + wire_digest(message.model_dump())[7:]
                self.episodes.record_user_turn(episode_id, message_id,
                                              wire_digest(message.content or ""), self.clock())
                ctx = pack.observe_user(message.content or "", ctx, user_turn_id=message_id,
                                        identity_values=identity_values(message.content or ""))
                data["latest_customer"] = {"id": message_id, "text": message.content or ""}
            elif message.role == "tool":
                pending = data["pending_call"]
                if (pending is None or message.call_id != pending["call_id"]
                        or message.tool_name not in {None, pending["tool_name"]}):
                    raise RuntimeStorageDenied("TOOL_RESULT_CORRELATION_FAILED")
                result, success = self._parse_result(message)
                self.episodes.consume_result(episode_id, pending["call_id"],
                                             wire_digest({"result": result, "success": success}),
                                             success, self.clock())
                proposal = ToolProposal.model_validate_json(wire_json(pending["proposal"]))
                ctx = pack.observe(proposal, result, ctx, success=success)
                if success:
                    data["successful_calls"][pending["call_id"]] = {
                        "tool_name": pending["effective_tool_name"],
                        "effect": "business_write" if pending["effect"] == "write" else pending["effect"],
                        "result_hash": wire_digest(result),
                    }
                message = message.model_copy(update={"tool_name": pending["tool_name"]})
                data["pending_call"] = None
            else:
                raise RuntimeStorageDenied("ASSISTANT_MESSAGE_CANNOT_BECOME_OBSERVATION")
            data["history"].append(message.model_dump(mode="json"))
        view = self.episodes.inspect(episode_id)
        obs_hash = wire_digest({"previous": view.observation_hash,
                                "incoming": observation.model_dump(mode="json")})
        view = self.episodes.record_observation(episode_id, obs_hash, view.state_version, self.clock())
        ctx = ctx.model_copy(update={"observation_hash": view.observation_hash,
                                     "state_version": view.state_version})
        data["domain_context"] = ctx.model_dump(mode="json")
        return ctx

    def _project(self, data: dict[str, Any], ctx: DomainContext, feedback: str | None) -> dict[str, Any]:
        vault = self._vault(data)
        # Register structured identity/resource facts before projecting earlier
        # free text or banking's line-oriented query output.
        facts = vault.project(ctx.entities, source="tool", subject_id=ctx.subject_id)
        projected = []
        calls: dict[str, tuple[int, dict[str, Any]]] = {}
        matched_calls: dict[int, int] = {}
        observations: list[ObservedResult] = []
        for index, raw in enumerate(data["history"]):
            item = dict(raw)
            source = "customer" if item["role"] == "user" else "tool"
            if item["role"] == "tool":
                prior = calls.pop(item["call_id"], None)
                if prior is None:
                    raise RuntimeStorageDenied("UNMATCHED_CONTEXT_RESULT")
                call_index, call = prior
                matched_calls[index] = call_index
                item["tool_name"] = call["tool_name"]
                result, success = self._parse_result(Message.model_validate(item))
                observations.append(ObservedResult(
                    item["call_id"], call["tool_name"], call.get("arguments") or {},
                    result, success, index,
                ))
                if isinstance(result, str) and (item.get("tool_name") or "").startswith("find_user_id"):
                    result = {"user_id": result}
                # Results must carry an explicit record owner or match trusted
                # observed entities; the current customer alone is no evidence
                # that every row in an ambiguous lookup belongs to them.
                item["content"] = vault.project(result, source=source, subject_id=None)
            else:
                if item["role"] == "assistant" and item.get("call_id") and item.get("tool_name"):
                    calls[item["call_id"]] = index, dict(raw)
                item["content"] = vault.project_text(item.get("content") or "", source=source,
                                                       subject_id=ctx.subject_id)
                if item.get("arguments"):
                    item["arguments"] = vault.project(item["arguments"], source="tool",
                                                        subject_id=ctx.subject_id)
            # Oversized retrieved data is bounded, with explicit loss metadata.
            encoded = wire_json(item).encode("utf-8")
            if item["role"] != "user" and len(encoded) > 24_576:
                text = wire_json(item.get("content"))
                item["content"] = {"truncated": True, "full_hash": wire_digest(item["content"]),
                                   "excerpt": text.encode("utf-8")[:20_000].decode("utf-8", "ignore")}
            projected.append(item)
        # A second pass removes known structured values from earlier free text,
        # including restored history. This does not assert universal PII detection.
        projected = vault.project(projected, source="history", subject_id=ctx.subject_id)

        # Retain an exact, projected pending proposal, not a model-written task
        # summary. Hashes bind the native values and are never text-redacted.
        pending_confirmation = None
        pending_action = None
        pending_operation = None
        active_arguments: list[Any] = []
        customer = vault.project({"user_id": ctx.subject_id}, source="tool", subject_id=ctx.subject_id)
        for field in ("pending_consent", "pending_call"):
            pending = data.get(field)
            if pending is None:
                continue
            proposal = ToolProposal.model_validate_json(wire_json(pending["proposal"]))
            active_arguments.append(proposal.arguments)
            pending_operation = (str(proposal.arguments.get("agent_tool_name"))
                                 if proposal.tool_name == "call_discoverable_agent_tool"
                                 else proposal.tool_name)
            binding = {
                "tool_name": proposal.tool_name,
                "arguments": vault.project(proposal.arguments, source="tool", subject_id=ctx.subject_id),
                "proposal_hash": proposal.digest, "arguments_hash": proposal.arguments_hash,
                "policy_hash": data["policy_hash"], "customer": customer,
            }
            if field == "pending_consent":
                pending_confirmation = {
                    **binding, "status": "awaiting_exact_customer_reply",
                    "displayed_text": vault.project_text(pending["displayed_text"], source="tool",
                                                          subject_id=ctx.subject_id),
                    "displayed_text_hash": wire_digest(pending["displayed_text"]),
                }
            else:
                prepared = self.episodes.get_call(data["episode_id"], pending["call_id"])
                pending_action = {
                    **binding, "call_id": pending["call_id"],
                    "status": "awaiting_correlated_tool_result",
                    "effective_tool_name": pending["effective_tool_name"],
                    "domain_admission_hash": pending["tal_decision_hash"],
                    "authorization_hash": sha256(prepared.authorization),
                    "consent_hash": prepared.call.consent_hash,
                    "observation_hash": prepared.call.observation_hash,
                    "action_hash": prepared.call.action_hash,
                }
        if observations:
            active_arguments.append(observations[-1].arguments)
        try:
            evidence = collect_evidence(
                observations, unlocked_tools=ctx.unlocked_tools,
                discovered_tools=ctx.discovered_tools, pending_operation=pending_operation,
            )
        except ContextRetentionDenied as exc:
            raise EpisodeInferenceDenied(str(exc)) from None

        def project_document(document: dict[str, Any]) -> dict[str, Any]:
            # Source offsets/hashes refer to the native observation, while text
            # is projected separately. Never run hashes/IDs through free-text
            # replacement: a short private value could coincidentally match one.
            return {
                **document,
                "title": (vault.project_text(document["title"], source="tool", subject_id=None)
                          if document["title"] is not None else None),
                "retrieval_query": (vault.project_text(document["retrieval_query"], source="tool",
                                                        subject_id=ctx.subject_id)
                                    if isinstance(document["retrieval_query"], str) else None),
                "spans": [{**span, "text": vault.project_text(span["text"], source="tool", subject_id=None)}
                          for span in document["spans"]],
            }

        definitions = [{**definition, "definition_text": vault.project_text(
            definition["definition_text"], source="tool", subject_id=None,
        )} for definition in evidence.unlocked_definitions]
        grants = [{**grant, "arguments": vault.project_text(
            grant["arguments"], source="tool", subject_id=ctx.subject_id,
        ), "instructions": vault.project_text(
            grant["instructions"], source="tool", subject_id=ctx.subject_id,
        )} for grant in evidence.user_tool_grants]

        def argument_strings(value: Any) -> set[str]:
            if isinstance(value, dict):
                result: set[str] = set()
                for key, child in value.items():
                    if key == "arguments" and isinstance(child, str):
                        try:
                            child = strict_json(child, max_bytes=65_536)
                        except ValueError:
                            pass
                    result.update(argument_strings(child))
                return result
            if isinstance(value, list):
                return set().union(*(argument_strings(child) for child in value))
            return {value} if isinstance(value, str) else set()

        active_values = argument_strings(active_arguments)
        required_fact_keys = {"user:" + str(ctx.subject_id), "bank:current_time"}
        required_fact_keys.update(key for key in ctx.entities if key.partition(":")[2] in active_values)
        # Projection preserves mapping order, but private entity keys become
        # opaque references. Zip binds each retained value to its native key.
        fact_entries = list(zip(ctx.entities, facts.items(), strict=True))
        required_facts = {key: value for native, (key, value) in fact_entries
                          if native in required_fact_keys}
        user_indices = [index for index, item in enumerate(projected) if item["role"] == "user"]
        anchors = {len(projected) - 1} if projected else set()
        if user_indices:
            anchors.update((user_indices[0], user_indices[-1]))
        if projected and len(projected) - 1 in matched_calls:
            anchors.add(matched_calls[len(projected) - 1])
        selected = {index: projected[index] for index in anchors}
        consent_binding = None if ctx.consent is None else {
            "proposal_hash": ctx.consent.proposal_digest, "policy_hash": ctx.consent.policy_hash,
            "confirmation_turn": ctx.consent.user_turn_id, "customer": customer,
        }
        base: dict[str, Any] = {
            "domain": data["domain"], "domain_policy": data["policy"],
            "tool_schemas": data["tools"], "authenticated": ctx.authenticated,
            "subject": {**customer, "authenticated": ctx.authenticated,
                        "policy_hash": ctx.policy_hash, "observation_hash": ctx.observation_hash},
            "observed_facts": required_facts,
            "successful_calls": data["successful_calls"],
            "pending_confirmation": pending_confirmation, "pending_action": pending_action,
            "consent_binding": consent_binding,
            "discovered_tools": list(ctx.discovered_tools),
            "unlocked_tool_definitions": definitions, "user_tool_grants": grants,
            "retrieval_documents": [project_document(document) for document in evidence.required_documents],
            "retrieval_inventory": dict(evidence.inventory),
            "privacy_profile": "synthetic_benchmark_field_policy_v1",
            "semantic_policy_verified": False,
            "feedback": feedback,
            "conversation": [selected[index] for index in sorted(selected)],
            "omitted_history_messages": len(projected) - len(selected),
            "context_retention": {
                "profile": CONTEXT_RETENTION_PROFILE,
                "anchor_history_indices": sorted(anchors),
                "omitted_fact_entries": len(facts) - len(required_facts),
                "observed_facts_hash": wire_digest(ctx.entities),
                "compacted_tool_history_indices": [],
            },
        }
        limit = self.limits.max_prompt_bytes
        # A retrieval/unlock result already retained above can be represented by
        # its source binding in the conversational copy. Its required excerpts
        # and complete unlocked definition are never dropped to make room.
        for observed in reversed(observations):
            if len(wire_json(base).encode("utf-8")) <= limit:
                break
            if observed.history_index not in selected or observed.operation not in {
                "KB_search", "KB_search_bm25", "KB_search_dense", "grep",
                "unlock_discoverable_agent_tool", "give_discoverable_user_tool",
            }:
                continue
            selected[observed.history_index] = {
                **selected[observed.history_index],
                "content": {"retained_in_evidence_fields": True, "source": observed.binding()},
            }
            base["context_retention"]["compacted_tool_history_indices"].append(observed.history_index)
            base["conversation"] = [selected[index] for index in sorted(selected)]
        if len(wire_json(base).encode()) > self.limits.max_prompt_bytes:
            raise EpisodeInferenceDenied("EPISODE_REQUIRED_CONTEXT_TOO_LARGE")
        for document in evidence.optional_documents:
            base["retrieval_inventory"]["retained_document_occurrences"] += 1
            base["retrieval_inventory"]["omitted_document_occurrences"] -= 1
            if not fit_optional(base, limit=limit, field="retrieval_documents", value=project_document(document)):
                base["retrieval_inventory"]["retained_document_occurrences"] -= 1
                base["retrieval_inventory"]["omitted_document_occurrences"] += 1
        for native, (key, value) in reversed(fact_entries):
            if native in required_fact_keys:
                continue
            base["observed_facts"][key] = value
            base["context_retention"]["omitted_fact_entries"] -= 1
            if len(wire_json(base).encode("utf-8")) > limit:
                del base["observed_facts"][key]
                base["context_retention"]["omitted_fact_entries"] += 1
        for index in reversed(range(len(projected))):
            if index in selected:
                continue
            selected[index] = projected[index]
            base["conversation"] = [selected[position] for position in sorted(selected)]
            base["omitted_history_messages"] = len(projected) - len(selected)
            if len(wire_json(base).encode("utf-8")) > limit:
                del selected[index]
        base["conversation"] = [selected[index] for index in sorted(selected)]
        base["omitted_history_messages"] = len(projected) - len(selected)
        if not selected or len(wire_json(base).encode("utf-8")) > limit:
            raise EpisodeInferenceDenied("EPISODE_OBSERVATION_CONTEXT_TOO_LARGE")
        return base

    def _infer(self, data: dict[str, Any], ctx: DomainContext,
               feedback: str | None) -> ToolCandidate | MessageCandidate:
        projected = self._project(data, ctx, feedback)
        now = self.clock()
        temperature = self.context.model_args.get("temperature", 0.0)
        seed = self._seed if self._seed is not None else self.context.model_args.get("seed")
        if (type(temperature) not in {int, float} or
                (seed is not None and type(seed) is not int)):
            raise ValueError("INVALID_EPISODE_MODEL_ARGUMENTS")
        request = EpisodeInferenceRequest(
            request_id="infer_" + uuid4().hex, episode_id=data["episode_id"], domain=ctx.domain,
            model=self.context.model, policy_hash=ctx.policy_hash,
            observation_hash=ctx.observation_hash, projected_context=projected,
            max_output_tokens=self.limits.max_output_tokens,
            temperature=float(cast(float, temperature)), seed=cast(int | None, seed),
            issued_at=now, deadline=min(now + timedelta(seconds=90),
                                       datetime.fromisoformat(data["deadline"])),
        )
        # UTF-8 byte bound plus a fixed system/schema allowance. These are held
        # units, not exact provider token usage or independently verified dollars.
        token_hold = len(request.model_dump_json().encode()) + 8192 + request.max_output_tokens
        try:
            candidate, reply = self.gateway.infer(
                request, token_hold=token_hold,
                cost_hold_microusd=self.limits.max_attempt_cost_microusd,
            )
        except EpisodeInferenceDenied as exc:
            if str(exc) == "EPISODE_INFERENCE_SCHEMA_REJECTED":
                _, stored_reply, _ = self.private.inference(request.request_id)
                if stored_reply is not None:
                    data["model_usage"].append({
                        "request_id": request.request_id,
                        "input_tokens": stored_reply.input_tokens,
                        "output_tokens": stored_reply.output_tokens,
                        "source": stored_reply.usage_source,
                        "reserved_cost_microusd": self.limits.max_attempt_cost_microusd,
                    })
            raise
        data["model_usage"].append({"request_id": request.request_id,
                                     "input_tokens": reply.input_tokens,
                                     "output_tokens": reply.output_tokens,
                                     "source": reply.usage_source,
                                     "reserved_cost_microusd": self.limits.max_attempt_cost_microusd})
        return candidate

    def _message(self, data: dict[str, Any], text: str, *, stop: bool = False) -> Decision:
        message_id = "response_" + uuid4().hex
        self.episodes.record_response(data["episode_id"], message_id, wire_digest(text),
                                      awaiting_customer=not stop, now=self.clock())
        data["history"].append(Message(role="assistant", content=text,
                                         message_id=message_id).model_dump(mode="json"))
        if stop:
            self.episodes.finish(data["episode_id"], EpisodeState.FINISHED, self.clock())
        return Decision(kind="message", content=text, stop=stop)

    def _tool(self, proposal: ToolProposal, ctx: DomainContext,
              data: dict[str, Any], *, consent_hash: str | None = None) -> Decision | str:
        view = self.episodes.inspect(data["episode_id"])
        ctx = ctx.model_copy(update={"state_version": view.state_version,
                                     "observation_hash": view.observation_hash,
                                     "consent": ctx.consent if consent_hash is not None else None})
        decision = self._pack(data).admit(proposal, ctx)
        # An affirmation is authority for this one admission only. Retaining it
        # across a tool result could authorize a second call with a new call ID.
        data["domain_context"] = ctx.model_copy(update={"consent": None}).model_dump(mode="json")
        if not decision.allowed:
            data["diagnostics"].append({"code": decision.reason_code,
                                         "proposal_hash": proposal.digest,
                                         "semantic_policy_verified": False})
            if decision.reason_code == "CUSTOMER_CONSENT_REQUIRED":
                displayed = confirmation_text(proposal.tool_name, proposal.arguments)
                data["pending_consent"] = {"proposal": proposal.model_dump(mode="json"),
                                             "displayed_text": displayed,
                                             "policy_hash": ctx.policy_hash,
                                             "subject_id": ctx.subject_id}
                return self._message(data, displayed)
            return decision.reason_code
        if decision.tal_decision_hash is None:
            raise RuntimeStorageDenied("MANDATORY_TAL_RECEIPT_MISSING")
        call = OutgoingCall(
            call_id="call_" + uuid4().hex, operation=proposal.tool_name,
            arguments_json=proposal.arguments_json, arguments_hash=proposal.arguments_hash,
            policy_hash=ctx.policy_hash, observation_hash=view.observation_hash,
            consent_hash=consent_hash,
        )
        auth = AuthorizationBinding(
            episode_id=data["episode_id"], action_hash=call.action_hash,
            policy_hash=call.policy_hash, observation_hash=call.observation_hash,
            state_version=view.state_version, consent_hash=consent_hash,
            domain_admission_hash=decision.tal_decision_hash,
        )
        prepared = self.episodes.prepare_call(data["episode_id"], call, auth,
                                              view.state_version, self.clock())
        if not prepared.emit_allowed:
            raise RuntimeStorageDenied("CALL_CANNOT_BE_EMITTED_AGAIN")
        data["pending_call"] = {"call_id": call.call_id, "tool_name": proposal.tool_name,
                                  "proposal": proposal.model_dump(mode="json"),
                                  "effect": decision.effect,
                                  "effective_tool_name": decision.effective_tool_name,
                                  "tal_decision_hash": decision.tal_decision_hash}
        data["pending_consent"] = None
        data["history"].append(Message(role="assistant", call_id=call.call_id,
                                         tool_name=call.operation, arguments=proposal.arguments,
                                         message_id=call.call_id).model_dump(mode="json"))
        return Decision(kind="tool", tool_name=call.operation, arguments=proposal.arguments,
                        call_id=call.call_id, authorization_id=sha256(auth))

    def _decide(self, data: dict[str, Any], ctx: DomainContext,
                observation: Observation) -> Decision:
        pending = data["pending_consent"]
        if pending and any(message.role == "user" for message in observation.messages):
            actual = data["latest_customer"]
            # The displayed proposal must have one immediate customer reply.
            # An intervening change/no followed by yes in the same batch cannot
            # silently approve the earlier proposal.
            choice = (classify_consent(actual["text"])
                      if len(observation.messages) == 1 else "ambiguous")
            data["pending_consent"] = None
            if choice == "no":
                return self._message(data, "Understood. I will leave those details as they are. "
                                     "What would you like to do next?")
            if (choice == "yes" and pending["subject_id"] == ctx.subject_id
                    and pending["policy_hash"] == ctx.policy_hash and ctx.subject_id is not None):
                proposal = ToolProposal.model_validate_json(wire_json(pending["proposal"]))
                consent_hash = consent_evidence(
                    episode_id=ctx.episode_id, proposal_digest=proposal.digest,
                    policy_hash=ctx.policy_hash, displayed_text=pending["displayed_text"],
                    user_turn_id=actual["id"], user_text=actual["text"],
                )
                ctx = ctx.model_copy(update={"consent": CustomerConsent(
                    episode_id=ctx.episode_id, proposal_digest=proposal.digest,
                    subject_id=ctx.subject_id, user_turn_id=actual["id"], policy_hash=ctx.policy_hash)})
                result = self._tool(proposal, ctx, data, consent_hash=consent_hash)
                if isinstance(result, Decision):
                    return result
        feedback: str | None = None
        for _ in range(self.limits.max_schema_repairs + 1):
            try:
                candidate = self._infer(data, ctx, feedback)
                if isinstance(candidate, ToolCandidate):
                    arguments = self._vault(data).resolve_arguments(
                        candidate.tool_name, candidate.arguments, now=self.clock(),
                        subject_id=ctx.subject_id,
                    )
                    proposal = ToolProposal(domain=ctx.domain, tool_name=candidate.tool_name,
                                            arguments=arguments)
                    result = self._tool(proposal, ctx, data)
                    if isinstance(result, Decision):
                        return result
                    feedback = result
                else:
                    text = render_response(
                        candidate, vault=self._vault(data), subject_id=ctx.subject_id,
                        authenticated=ctx.authenticated, successful_calls=data["successful_calls"],
                        now=self.clock(),
                    )
                    return self._message(data, text, stop=candidate.stop)
            except EpisodeInferenceDenied as exc:
                if str(exc) != "EPISODE_INFERENCE_SCHEMA_REJECTED":
                    raise
                feedback = "EPISODE_INFERENCE_SCHEMA_REJECTED"
            except (ProjectionDenied, ResponseDenied) as exc:
                feedback = str(exc)
            data["diagnostics"].append({"code": feedback, "repair": True})
        return self._message(data, "I need more information before I can continue. "
                             "Could you clarify what you would like me to do?")

    def step(self, observation: Observation, state: Any) -> tuple[Decision, dict[str, Any]]:
        if (not isinstance(state, dict) or set(state) != {"episode_id", "version"}
                or not isinstance(state["episode_id"], str)
                or type(state["version"]) is not int or state["version"] < 0):
            raise RuntimeStorageDenied("INVALID_RUNTIME_STATE")
        observation = Observation.model_validate_json(observation.model_dump_json())
        episode_id = state["episode_id"]
        view = self._owned_episode(episode_id)
        loaded = self.private.load(episode_id)
        if loaded.version != state["version"]:
            raise RuntimeStorageDenied("STALE_RUNTIME_STATE")
        self._validate_sources(loaded.data, view)
        lease = self.private.claim(episode_id, observation.observation_id, loaded.version)
        data = lease.data
        usage_start = len(data["model_usage"])
        ctx = self._observe(observation, data)
        # Exceptions retain the lease and any charged/pending operation. There
        # is deliberately no automatic retry after an ambiguous boundary.
        decision = self._decide(data, ctx, observation)
        usage = data["model_usage"][usage_start:]
        if usage:
            decision = decision.model_copy(update={"usage": {
                "input_tokens": sum(item["input_tokens"] for item in usage),
                "output_tokens": sum(item["output_tokens"] for item in usage),
                "safe_model_attempts": len(usage),
                "safe_reserved_cost_microusd": sum(item["reserved_cost_microusd"] for item in usage),
                "safe_usage_source": sorted({item["source"] for item in usage}),
                "safe_billing_verified": False,
            }})
        data["seen_observations"].append(observation.observation_id)
        self.private.save(episode_id, lease, data)
        return decision, {"episode_id": episode_id, "version": lease.version + 1}

    def close_episode(self, state: Any, reason: str = "harness_stop") -> None:
        """Stop new work without converting an unknown external effect to success."""
        if not isinstance(state, dict) or not isinstance(state.get("episode_id"), str):
            raise RuntimeStorageDenied("INVALID_RUNTIME_STATE")
        episode_id = state["episode_id"]
        view = self._owned_episode(episode_id)
        if view.state in {EpisodeState.FINISHED, EpisodeState.FAILED, EpisodeState.HANDOFF}:
            return
        if view.pending_call_id:
            if view.state is not EpisodeState.OUTCOME_UNCERTAIN:
                self.episodes.mark_call_uncertain(episode_id, view.pending_call_id,
                                                  sha256("HARNESS_STOP_PENDING_TOOL"), self.clock())
            return
        if view.pending_request_id:
            if view.state is not EpisodeState.OUTCOME_UNCERTAIN:
                self.episodes.mark_inference_uncertain(
                    episode_id, view.pending_request_id, sha256("HARNESS_STOP_PENDING_MODEL"), self.clock())
            return
        self.episodes.finish(episode_id, EpisodeState.FAILED, self.clock())


def runtime_factory(context: RuntimeContext) -> EpisodeRuntime:
    """Reviewed hosted benchmark profile; no provider secret enters this process."""
    from safe_agent.inference_service.__main__ import _secret_from_file
    from safe_agent.tal.domain_runtime import require_reviewed_domain_runtime

    require_reviewed_domain_runtime()
    output = Path(context.output_dir).resolve()
    private = output / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = private.lstat()
    if (private.resolve() != private or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("BENCHMARK_PRIVATE_DIRECTORY_DENIED")
    database = private / "episodes.sqlite"
    descriptor = os.open(database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("BENCHMARK_PRIVATE_DATABASE_DENIED")
    finally:
        os.close(descriptor)
    engine = create_engine("sqlite+pysqlite:///" + str(database),
                           connect_args={"timeout": 30})
    EpisodeRuntime.create_fixture_schema(engine)
    key = _secret_from_file(Path(os.environ["SAFE_EPISODE_DISPATCH_KEY_FILE"]))
    transport = EpisodeUnixTransport(Path(os.environ["SAFE_EPISODE_SOCKET"]))
    return EpisodeRuntime(context, engine=engine, transport=transport,
                          authenticator=EpisodeDispatchAuthenticator(key))


build_runtime = runtime_factory
