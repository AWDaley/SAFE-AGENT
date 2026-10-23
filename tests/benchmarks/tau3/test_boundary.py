from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from safe_agent.benchmarks.tau3.adapter import AdapterProtocolError, SafeTauAgent
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.protocol import Decision


class NeverExecuteTool:
    openai_schema = {
        "type": "function",
        "function": {"name": "update", "description": "fixture", "parameters": {"type": "object"}},
    }

    def __call__(self, **kwargs):
        raise AssertionError("The adapter must not execute a tool callable")


class RecordingRuntime:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.observations = []
        self.initialization = None

    def start_episode(self, **kwargs):
        self.initialization = kwargs
        return 0

    def step(self, observation, state):
        self.observations.append(observation)
        return next(self.decisions), state + 1


def user(content="Please update.", turn_idx=1):
    return SimpleNamespace(role="user", content=content, turn_idx=turn_idx, timestamp="fixed")


def result(call_id="call_1", *, requestor="assistant", error=False):
    return SimpleNamespace(
        role="tool",
        content='{"ok":true}',
        id=call_id,
        requestor=requestor,
        error=error,
        timestamp="later",
        turn_idx=2,
    )


def call(call_id="call_1"):
    return Decision(
        kind="tool",
        tool_name="update",
        arguments={"value": "new"},
        call_id=call_id,
        authorization_id="auth_1",
    )


def test_adapter_emits_once_correlates_result_and_does_not_execute():
    runtime = RecordingRuntime([call(), Decision(kind="message", content="Updated.", stop=True)])
    agent = SafeTauAgent(
        tools=[NeverExecuteTool()], domain_policy="policy", domain="retail", runtime=runtime
    )
    state = agent.get_init_state()
    assert state.episode_id.startswith("ep_")
    assert agent.next_decision(user(), state).kind == "tool"
    assert state.pending == {"call_1": "update"}
    assert agent.next_decision(result(), state).stop
    assert runtime.observations[-1].messages[0].tool_name == "update"
    assert not state.pending
    with pytest.raises(AdapterProtocolError, match="terminated"):
        agent.next_decision(user("again", 3), state)


def test_duplicate_result_cannot_reemit_write_or_trigger_more_inference():
    runtime = RecordingRuntime([call(), Decision(kind="message", content="Updated.")])
    agent = SafeTauAgent(
        tools=[NeverExecuteTool()], domain_policy="p", domain="retail", runtime=runtime
    )
    state = agent.get_init_state()
    agent.next_decision(user(), state)
    agent.next_decision(result(), state)
    with pytest.raises(AdapterProtocolError, match="already consumed"):
        agent.next_decision(result(), state)
    assert len(runtime.observations) == 2


def test_reused_call_id_and_unmatched_result_fail_closed():
    runtime = RecordingRuntime([call(), call()])
    agent = SafeTauAgent(
        tools=[NeverExecuteTool()], domain_policy="p", domain="retail", runtime=runtime
    )
    state = agent.get_init_state()
    with pytest.raises(AdapterProtocolError, match="pending"):
        agent.next_decision(result(), state)
    agent.next_decision(user(), state)
    with pytest.raises(AdapterProtocolError, match="twice"):
        agent.next_decision(result(), state)


def test_user_tools_and_privileged_raw_data_do_not_cross_boundary():
    runtime = RecordingRuntime([Decision(kind="message", content="How can I help?")])
    agent = SafeTauAgent(tools=[], domain_policy="public policy", domain="retail", runtime=runtime)
    hidden = SimpleNamespace(role="user", content="private user action", tool_calls=[object()])
    state = agent.get_init_state([hidden, result("user_call", requestor="user")])
    assert runtime.initialization["history"] == []
    incoming = user("Visible request")
    incoming.raw_data = {"gold_actions": "PRIVATE_SENTINEL"}
    agent.next_decision(incoming, state)
    assert "PRIVATE_SENTINEL" not in runtime.observations[0].model_dump_json()
    with pytest.raises(AdapterProtocolError, match="User tool calls"):
        agent.next_decision(hidden, state)


def test_multi_result_history_is_reconciled_without_exposing_user_results():
    runtime = RecordingRuntime([Decision(kind="message", content="Recorded.")])
    agent = SafeTauAgent(
        tools=[NeverExecuteTool()], domain_policy="p", domain="retail", runtime=runtime
    )
    historical_call = SimpleNamespace(
        role="assistant",
        content=None,
        tool_calls=[
            SimpleNamespace(id="old_call", name="update", arguments={}, requestor="assistant")
        ],
    )
    state = agent.get_init_state([historical_call])
    assert state.pending == {"old_call": "update"}
    multi = SimpleNamespace(role="tool", tool_messages=[result("old_call")])
    agent.next_decision(multi, state)
    assert not state.pending


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "tool", "tool_name": "update", "arguments": {}, "call_id": "c"},
        {"kind": "message", "content": "ok", "tool_name": "update"},
        {"kind": "message", "content": ""},
        {
            "kind": "tool",
            "tool_name": "update",
            "arguments": {},
            "call_id": "c",
            "authorization_id": "a",
            "stop": True,
        },
    ],
)
def test_invalid_decision_never_reaches_dispatch(kwargs):
    with pytest.raises(ValidationError):
        Decision.model_validate(kwargs)


def test_final_configs_cannot_omit_tasks_or_fake_banking_holdout():
    with pytest.raises(ValidationError):
        BenchmarkConfig(stage="final", num_trials=4, num_tasks=5)
    with pytest.raises(ValidationError):
        BenchmarkConfig(domain="banking_knowledge", task_split_name="test")
    with pytest.raises(ValidationError):
        BenchmarkConfig(stage="final", num_trials=1)
    with pytest.raises(ValidationError, match="canonical"):
        BenchmarkConfig(stage="final", num_trials=4, runtime_factory="fixture:runtime")
