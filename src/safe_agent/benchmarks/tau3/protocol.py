"""JSON-only boundary between the official harness and SAFE's episode runtime.

Neither tool callables nor a benchmark Task may cross this boundary. The harness
executes a returned authorized call exactly once and supplies its result later.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from safe_agent.contracts.episode_inference import LocalTimingProfile


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ToolSpec(BoundaryModel):
    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, JsonValue]
    tool_type: str | None = None
    mutates_state: bool | None = None


class Message(BoundaryModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, JsonValue] | None = None
    error: bool = False
    message_id: str | None = None
    turn_index: int | None = None

    @model_validator(mode="after")
    def coherent_message(self) -> Self:
        if self.role == "tool" and not self.call_id:
            raise ValueError("A tool result must identify the originating call")
        if self.role == "user" and (self.call_id or self.tool_name or self.arguments is not None):
            raise ValueError("User tool calls are private to the official user simulator")
        if self.arguments is not None and (not self.tool_name or not self.call_id):
            raise ValueError("An assistant call requires its name and identifier")
        return self


class Observation(BoundaryModel):
    observation_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)


class Decision(BoundaryModel):
    kind: Literal["message", "tool"]
    content: str | None = None
    tool_name: str | None = None
    arguments: dict[str, JsonValue] | None = None
    call_id: str | None = None
    authorization_id: str | None = None
    stop: bool = False
    cost: float | None = Field(default=None, ge=0)
    usage: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def exactly_one_output(self) -> Self:
        if self.kind == "message":
            if not self.content or not self.content.strip():
                raise ValueError("An assistant message must have nonempty content")
            if any(
                x is not None
                for x in (self.tool_name, self.arguments, self.call_id, self.authorization_id)
            ):
                raise ValueError("A message cannot also dispatch a tool")
        else:
            if self.content is not None or self.stop:
                raise ValueError("A tool call cannot also speak or stop")
            if not self.tool_name or not self.call_id or not self.authorization_id:
                raise ValueError("Dispatch requires a tool, call ID, and authorization ID")
            if self.arguments is None:
                raise ValueError("Dispatch requires an explicit argument object")
        return self


class EpisodeRuntimeProtocol(Protocol):
    def start_episode(
        self,
        *,
        episode_id: str,
        domain: str,
        policy: str,
        tools: list[ToolSpec],
        history: list[Message],
    ) -> Any: ...

    def step(self, observation: Observation, state: Any) -> tuple[Decision, Any]: ...


class RuntimeContext(BoundaryModel):
    """Run-level setup for a factory; deliberately contains no benchmark task."""

    run_id: str
    domain: Literal["retail", "banking_knowledge"]
    model: str
    model_args: dict[str, JsonValue] = Field(default_factory=dict)
    episode_budget: dict[str, JsonValue] = Field(default_factory=dict)
    run_budget: dict[str, JsonValue] = Field(default_factory=dict)
    local_timing: LocalTimingProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    output_dir: str


class RuntimeFactory(Protocol):
    def __call__(self, context: RuntimeContext) -> EpisodeRuntimeProtocol: ...
