"""Bounded failed repairs do not become repeated conversation loops or success."""

import pytest

from safe_agent.contracts.episodes import EpisodeState
from tests.episodes.test_episode_runtime_audit import customer, runtime_fixture, start


@pytest.mark.parametrize("candidate", [
    '{"kind":"message","purpose":"answer","text":"I have transferred the funds."}',
    '{"kind":"tool","tool_name":"not_an_allowed_tool","arguments":{}}',
])
def test_identical_denied_candidate_stops_failed_within_existing_repair_allowance(tmp_path, candidate):
    runtime, transport, _, tools, engine = runtime_fixture(tmp_path, lambda _: candidate)
    try:
        decision, state = runtime.step(customer("Help with a request.", "obs_1"), start(runtime, tools))
        assert decision.stop is True and decision.kind == "message"
        assert runtime.episodes.inspect(state["episode_id"]).state is EpisodeState.FAILED
        assert len(transport.requests) == 2
        assert runtime.episodes.inspect(state["episode_id"]).usage.tool_calls == 0
        diagnostics = runtime.private.load(state["episode_id"]).data["diagnostics"]
        assert any(item["code"] == "REPEATED_REPAIR_WITHOUT_PROGRESS" for item in diagnostics)
        assert "without claiming" in decision.content
    finally:
        engine.dispose()


def test_distinct_invalid_repairs_exhaust_without_new_model_attempt_or_success(tmp_path):
    replies = iter([
        '{"kind":"message","purpose":"success","text":"Done."}',
        '{"kind":"message","purpose":"answer","text":"Your transfer has been processed."}',
    ])
    runtime, transport, _, tools, engine = runtime_fixture(tmp_path, lambda _: next(replies))
    try:
        decision, state = runtime.step(customer("Help.", "obs_1"), start(runtime, tools))
        assert decision.stop and len(transport.requests) == 2
        assert runtime.episodes.inspect(state["episode_id"]).state is EpisodeState.FAILED
        assert runtime.private.load(state["episode_id"]).data["diagnostics"][-1]["code"] == (
            "REPAIR_ALLOWANCE_EXHAUSTED"
        )
    finally:
        engine.dispose()


def test_legitimate_repeated_question_after_new_customer_turn_is_not_stopped(tmp_path):
    runtime, transport, _, tools, engine = runtime_fixture(tmp_path)
    try:
        state = start(runtime, tools)
        first, state = runtime.step(customer("Hello.", "obs_1"), state)
        second, state = runtime.step(customer("I have not given my request yet.", "obs_2"), state)
        assert first.content == second.content
        assert not first.stop and not second.stop
        assert len(transport.requests) == 2
        assert runtime.episodes.inspect(state["episode_id"]).state is EpisodeState.AWAITING_CUSTOMER
    finally:
        engine.dispose()
