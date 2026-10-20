"""Half-duplex bridge. Only τ's orchestrator executes returned tool calls."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .protocol import Decision, EpisodeRuntimeProtocol, Message, Observation, ToolSpec


class AdapterProtocolError(RuntimeError):
    """The harness/runtime exchange cannot safely continue."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def tool_specs(tools: list[Any]) -> list[ToolSpec]:
    """Copy only public schema and declared effect metadata; retain no callable."""
    specs = []
    for tool in tools:
        schema = tool.openai_schema
        function = schema["function"] if "function" in schema else schema
        implementation = getattr(tool, "_func", None)
        tool_type = getattr(implementation, "__tool_type__", None)
        tool_type = getattr(tool_type, "value", tool_type)
        mutates_state = getattr(implementation, "__mutates_state__", None)
        specs.append(
            ToolSpec.model_validate(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                    "tool_type": tool_type,
                    "mutates_state": mutates_state,
                }
            )
        )
    if len({spec.name for spec in specs}) != len(specs):
        raise AdapterProtocolError("Duplicate tool names in the environment")
    return specs


@dataclass
class AdapterState:
    episode_id: str
    runtime_state: Any
    pending: dict[str, str] = field(default_factory=dict)
    seen_call_ids: set[str] = field(default_factory=set)
    consumed_results: dict[str, str] = field(default_factory=dict)
    seen_observations: set[str] = field(default_factory=set)
    stopped: bool = False


def _visible_messages(raw: Any, *, history: bool, pending: dict[str, str]) -> list[Message]:
    # Deliberately do not model_dump the upstream object: raw_data, user tool
    # calls, audio metadata, and evaluator fields have no place in SAFE input.
    if hasattr(raw, "tool_messages"):
        result = []
        for message in raw.tool_messages:
            result.extend(_visible_messages(message, history=history, pending=pending))
        return result
    role = getattr(raw, "role", None)
    if getattr(raw, "is_audio", False) or getattr(raw, "audio_content", None):
        raise AdapterProtocolError("This adapter supports text episodes only")
    if role == "system":
        if history:
            return []
        raise AdapterProtocolError("The runtime cannot receive a new system authority message")
    calls = getattr(raw, "tool_calls", None)
    if role == "user" and calls:
        if history:
            return []
        raise AdapterProtocolError("User tool calls must stay with the official simulator")
    if role == "tool" and getattr(raw, "requestor", "assistant") != "assistant":
        if history:
            return []
        raise AdapterProtocolError("A user-only tool result was delivered to the agent")
    content = getattr(raw, "content", None)
    turn_index = getattr(raw, "turn_idx", None)
    timestamp = getattr(raw, "timestamp", None)
    identity = {"role": role, "content": content, "turn_index": turn_index, "timestamp": timestamp}
    if role == "tool":
        call_id = str(raw.id)
        return [
            Message(
                role="tool",
                content=content,
                call_id=call_id,
                tool_name=pending.get(call_id),
                error=bool(raw.error),
                turn_index=turn_index,
                message_id=_digest({**identity, "call_id": call_id, "error": bool(raw.error)}),
            )
        ]
    if role == "assistant" and calls:
        if not history:
            raise AdapterProtocolError("An assistant output is not an input observation")
        result = []
        for call in calls:
            if getattr(call, "requestor", "assistant") != "assistant":
                raise AdapterProtocolError("An assistant history call has a user requestor")
            pending[call.id] = call.name
            result.append(
                Message(
                    role="assistant",
                    call_id=call.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    turn_index=turn_index,
                    message_id=_digest(
                        {**identity, "call_id": call.id, "arguments": call.arguments}
                    ),
                )
            )
        return result
    if role not in {"user", "assistant"}:
        raise AdapterProtocolError("Unsupported message role")
    if role == "assistant" and not history:
        raise AdapterProtocolError("An assistant output is not an input observation")
    # Real τ messages carry timestamps. A caller without stable identity cannot
    # claim replay guarantees for a repeated natural-language message.
    message_id = (
        _digest(identity) if timestamp is not None or turn_index is not None else str(uuid.uuid4())
    )
    return [Message(role=role, content=content, turn_index=turn_index, message_id=message_id)]


class SafeTauAgent:
    """Protocol implementation, kept importable without optional τ dependencies."""

    def __init__(
        self,
        *,
        tools: list[Any],
        domain_policy: str,
        domain: str,
        runtime: EpisodeRuntimeProtocol,
        episode_id: str | None = None,
    ):
        self.domain = domain
        self.domain_policy = domain_policy
        self.specs = tool_specs(tools)
        self.runtime = runtime
        if episode_id is not None and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", episode_id
        ):
            raise ValueError(
                "Episode IDs must begin with a letter and contain safe identifier characters"
            )
        self._episode_id = episode_id

    def get_init_state(self, message_history: list[Any] | None = None) -> AdapterState:
        episode_id = self._episode_id or "ep_" + uuid.uuid4().hex
        pending: dict[str, str] = {}
        history = []
        seen_calls: set[str] = set()
        consumed = {}
        for raw in message_history or []:
            for message in _visible_messages(raw, history=True, pending=pending):
                history.append(message)
                if message.role == "assistant" and message.call_id:
                    if message.call_id in seen_calls:
                        raise AdapterProtocolError("Duplicate call ID in pre-existing history")
                    seen_calls.add(message.call_id)
                if message.role == "tool":
                    if message.call_id not in pending:
                        raise AdapterProtocolError("History contains an unmatched tool result")
                    pending.pop(message.call_id)
                    consumed[message.call_id] = _digest(message.model_dump())
        state = self.runtime.start_episode(
            episode_id=episode_id,
            domain=self.domain,
            policy=self.domain_policy,
            tools=[ToolSpec.model_validate_json(spec.model_dump_json()) for spec in self.specs],
            history=history,
        )
        return AdapterState(
            episode_id=episode_id,
            runtime_state=state,
            pending=pending,
            seen_call_ids=seen_calls,
            consumed_results=consumed,
        )

    def next_decision(self, message: Any, state: AdapterState) -> Decision:
        if state.stopped:
            raise AdapterProtocolError("The episode has already terminated")
        visible = _visible_messages(message, history=False, pending=state.pending)
        if not visible:
            raise AdapterProtocolError("No agent-visible input was supplied")
        result_ids = [item.call_id for item in visible if item.role == "tool"]
        if len(result_ids) != len(set(result_ids)):
            raise AdapterProtocolError("The same result appears twice in one observation")
        for item in visible:
            if item.role == "tool":
                if item.call_id in state.consumed_results:
                    raise AdapterProtocolError(
                        "A tool result was already consumed; refusing to redispatch"
                    )
                if item.call_id not in state.pending:
                    raise AdapterProtocolError(
                        "The tool result does not match a pending authorized call"
                    )
        observation = Observation(
            observation_id=_digest([item.model_dump() for item in visible]),
            messages=visible,
        )
        if observation.observation_id in state.seen_observations:
            raise AdapterProtocolError("Duplicate observation; refusing to repeat a prior dispatch")
        # Consume before inference. An exception ends this attempt; it must not
        # re-issue an ambiguous write or silently replay the same observation.
        state.seen_observations.add(observation.observation_id)
        for item in visible:
            if item.role == "tool":
                assert item.call_id is not None
                state.pending.pop(item.call_id)
                state.consumed_results[item.call_id] = _digest(item.model_dump())
        decision, runtime_state = self.runtime.step(observation, state.runtime_state)
        if not isinstance(decision, Decision):
            raise AdapterProtocolError("EpisodeRuntime must return a strict Decision")
        decision = Decision.model_validate_json(decision.model_dump_json())
        state.runtime_state = runtime_state
        if decision.kind == "tool":
            assert decision.call_id is not None and decision.tool_name is not None
            if state.pending:
                raise AdapterProtocolError("A prior tool outcome remains unresolved")
            if decision.tool_name not in {spec.name for spec in self.specs}:
                raise AdapterProtocolError("Runtime selected a tool not exposed by the environment")
            if decision.call_id in state.seen_call_ids:
                raise AdapterProtocolError("A call ID cannot be dispatched twice")
            state.seen_call_ids.add(decision.call_id)
            state.pending[decision.call_id] = decision.tool_name
        if decision.stop:
            if state.pending:
                raise AdapterProtocolError("Cannot declare completion with an unresolved call")
            state.stopped = True
        return decision

    def generate_next_message(self, message: Any, state: AdapterState) -> tuple[Any, AdapterState]:
        from tau2.data_model.message import (  # type: ignore[import-untyped]
            AssistantMessage,
            ToolCall,
        )

        decision = self.next_decision(message, state)
        if decision.kind == "tool":
            output = AssistantMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id=decision.call_id,
                        name=decision.tool_name,
                        arguments=decision.arguments,
                        requestor="assistant",
                    )
                ],
                cost=decision.cost,
                usage=decision.usage,
                raw_data={
                    "safe_authorization_id": decision.authorization_id,
                    "safe_episode_id": state.episode_id,
                },
            )
        else:
            output = AssistantMessage(
                role="assistant",
                content=decision.content,
                cost=decision.cost,
                usage=decision.usage,
                raw_data={"safe_stop": decision.stop, "safe_episode_id": state.episode_id},
            )
        return output, state

    @classmethod
    def is_stop(cls, message: Any) -> bool:
        return (getattr(message, "raw_data", None) or {}).get("safe_stop") is True

    def set_seed(self, seed: int) -> None:
        setter = getattr(self.runtime, "set_seed", None)
        if setter is not None:
            setter(seed)

    def stop(self, message: Any = None, state: AdapterState | None = None) -> None:
        if state is not None:
            state.stopped = True
            close_episode = getattr(self.runtime, "close_episode", None)
            if close_episode is not None:
                close_episode(state.runtime_state, reason="harness_stop")


def official_agent_class() -> type[SafeTauAgent]:
    """Instantiate the actual upstream ABC only when the harness is installed."""
    from tau2.agent.base_agent import HalfDuplexAgent  # type: ignore[import-untyped]

    class OfficialSafeTauAgent(SafeTauAgent, HalfDuplexAgent):
        pass

    return OfficialSafeTauAgent
