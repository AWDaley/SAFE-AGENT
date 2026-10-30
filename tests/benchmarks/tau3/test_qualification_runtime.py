"""Authored offline qualification wiring; no model readiness or benchmark score.

The official orchestrator, BM25 environment and SAFE runtime are real. Inference
and the customer utterances are explicit fixtures; no judge is allowed to run.
"""

import hashlib
import json
import re
import stat

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.protocol import RuntimeContext
from safe_agent.benchmarks.tau3.qualification import Probe, _run_agent_probe


@pytest.fixture
def runtime_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm
    import tau2.evaluator.evaluator
    import tau2.runner.simulation
    from tal.domain_admission import admit_domain_action

    from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
    from safe_agent.orchestration.episode_runtime import EpisodeRuntime

    def no_provider(*args, **kwargs):
        raise AssertionError("Qualification wiring fixtures must not call a provider or embeddings")

    def no_evaluator(*args, **kwargs):
        raise AssertionError("Authored qualification must not invoke a benchmark evaluator")

    for name in ("completion", "acompletion", "embedding", "aembedding"):
        monkeypatch.setattr(litellm, name, no_provider)
    monkeypatch.setattr(tau2.evaluator.evaluator, "evaluate_simulation", no_evaluator)
    monkeypatch.setattr(tau2.runner.simulation, "evaluate_simulation", no_evaluator)
    config = BenchmarkConfig(
        run_id="qualification_authored_runtime_fixture",
        domain="banking_knowledge",
        model_route="local",
        local_api_base="http://127.0.0.1:8080/v1",
        llm_agent="authored-scripted-model",
        llm_user="authored-scripted-model",
        local_timing={
            "provider_timeout_seconds": 20,
            "transport_timeout_seconds": 25,
            "request_timeout_seconds": 30,
        },
        output_dir=str(tmp_path / "unused_benchmark_output"),
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    engines = []

    def create(propose, *, episode_budget=None, run_budget=None):
        engine = create_engine("sqlite://")
        engines.append(engine)
        EpisodeRuntime.create_fixture_schema(engine)
        transport = ScriptedEpisodeTransport(propose)
        runtime = EpisodeRuntime(
            RuntimeContext(
                run_id=config.run_id,
                domain=config.domain,
                model=config.llm_agent,
                model_args=config.llm_args_agent,
                output_dir=str(tmp_path),
                local_timing=config.local_timing,
                episode_budget=episode_budget or {},
                run_budget=run_budget or {},
            ),
            engine=engine,
            transport=transport,
            admission_evaluator=admit_domain_action,
        )
        return runtime, transport

    yield config, private, create
    for engine in engines:
        engine.dispose()


def assert_trace_and_completed_requests(result, private, runtime, transport, count):
    assert result["passed"], result["issues"]
    assert result["issues"] == []
    assert len(transport.requests) == len(result["requests"]) == count
    assert all(record["status"] == "COMPLETE" for record in result["requests"])
    assert all(record["role"] == "agent" for record in result["requests"])
    assert all(record["seconds"] >= 0 for record in result["requests"])
    for record in result["requests"]:
        request, reply, status = runtime.private.inference(record["request_id"])
        assert status == "COMPLETE"
        assert request.episode_id == result["episode_id"]
        assert reply is not None and reply.usage_source == "scripted"
    trace_path = private / (result["probe_id"] + ".json")
    raw = trace_path.read_bytes()
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600
    assert hashlib.sha256(raw).hexdigest() == result["trace_sha256"]
    trace = json.loads(raw)
    assert "error_code" not in trace
    assert "reward_info" not in trace and "reward_info" not in result
    assert all("raw_data" not in message for message in trace["messages"])
    return trace


@pytest.mark.parametrize("history_pairs", [0, 32])
def test_authored_clarification_uses_real_runtime_and_one_complete_request(
    runtime_fixture, history_pairs
):
    config, private, create = runtime_fixture
    answer = "Do monthly fees or interest rates matter more to you?"
    runtime, transport = create(
        [json.dumps({"kind": "message", "purpose": "question", "text": answer})]
    )
    probe = Probe(
        f"agent_clarification_history{history_pairs}", "agent", "clarification", history_pairs
    )
    result = _run_agent_probe(probe, config, runtime, private)
    trace = assert_trace_and_completed_requests(result, private, runtime, transport, 1)
    assert result["tool_calls"] == result["successful_tools"] == []
    assert [
        message["content"] for message in trace["messages"] if message["role"] == "assistant"
    ] == [answer]
    assert result["response_sha256"] == hashlib.sha256(answer.encode()).hexdigest()
    context = transport.requests[0].projected_context
    assert context["omitted_history_messages"] == 0
    assert len(context["conversation"]) == history_pairs * 2 + 1
    assert "required_documents" not in transport.requests[0].model_dump_json()


def test_authored_kb_search_and_answer_trace_real_bm25_success_without_evaluation(runtime_fixture):
    config, private, create = runtime_fixture
    answers = []

    def propose(request):
        context = request.projected_context
        receipts = context["successful_calls"]
        if not receipts:
            return json.dumps(
                {
                    "kind": "tool",
                    "tool_name": "KB_search",
                    "arguments": {"query": "closing a checking account"},
                }
            )
        assert any(receipt["tool_name"] == "KB_search" for receipt in receipts.values())
        observed = [
            message
            for message in context["conversation"]
            if message["role"] == "tool" and message["tool_name"] == "KB_search"
        ]
        assert len(observed) == 1 and observed[0]["content"]
        # Copy a substantive line from the actual read result, without a task-specific gold answer.
        content = observed[0]["content"]
        try:
            content = json.loads(content)
        except ValueError:
            pass
        assert isinstance(content, str)
        rule = next(line.strip() for line in content.splitlines()
                    if re.search(r"\b(?:must|ensure|verify|required|before|balance)\b", line, re.I)
                    and len(re.findall(r"\b[a-zA-Z]+\b", line)) >= 7)
        answer = "For closing a checking account, the retrieved policy states: " + rule
        answers.append(answer)
        return json.dumps({"kind": "message", "purpose": "answer", "text": answer})

    runtime, transport = create(propose)
    result = _run_agent_probe(
        Probe("agent_grounding_history0", "agent", "grounding", 0), config, runtime, private
    )
    trace = assert_trace_and_completed_requests(result, private, runtime, transport, 2)
    assert result["tool_calls"] == result["successful_tools"] == ["KB_search"]
    tool_results = [message for message in trace["messages"] if message["role"] == "tool"]
    assert len(tool_results) == 1 and tool_results[0]["error"] is False
    assert tool_results[0]["content"]
    assert [
        message["content"]
        for message in trace["messages"]
        if message["role"] == "assistant" and not message.get("tool_calls")
    ] == answers
    durable = runtime.private.load(result["episode_id"]).data
    assert len(durable["successful_calls"]) == 1
    assert next(iter(durable["successful_calls"].values()))["tool_name"] == "KB_search"


def test_search_promise_stops_on_first_normal_message_without_dispatching_next_candidate(runtime_fixture):
    config, private, create = runtime_fixture
    promise = (
        "I understand the bank's policy requires checking the knowledge base for relevant information. "
        "Let me search the knowledge base to find the current policy regarding closing a checking account. "
        "Please be patient while I retrieve the information."
    )
    runtime, transport = create([
        json.dumps({"kind": "message", "purpose": "user_instruction", "text": promise}),
        json.dumps({"kind": "tool", "tool_name": "KB_search",
                    "arguments": {"query": "closing a checking account"}}),
    ])
    result = _run_agent_probe(
        Probe("agent_grounding_history0", "agent", "grounding", 0), config, runtime, private,
    )
    assert result["passed"] is False
    assert "RETRIEVAL_EVIDENCE_MISSING" in result["issues"]
    assert "RETRIEVAL_ANSWER_NOT_GROUNDED" in result["issues"]
    assert len(transport.requests) == len(result["requests"]) == 1
    assert result["requests"][0]["status"] == "COMPLETE"
    assert result["tool_calls"] == result["successful_tools"] == []
    trace = json.loads((private / (result["probe_id"] + ".json")).read_bytes())
    assert [message["content"] for message in trace["messages"]
            if message["role"] == "assistant"] == [promise]


def test_real_retrieval_receipt_without_a_substantive_answer_does_not_qualify(runtime_fixture):
    config, private, create = runtime_fixture
    runtime, transport = create([
        json.dumps({"kind": "tool", "tool_name": "KB_search",
                    "arguments": {"query": "closing a checking account"}}),
        json.dumps({"kind": "message", "purpose": "answer",
                    "text": "The retrieved policy covers checking-account closure. "
                            "Which checking account would you like help with?"}),
    ])
    result = _run_agent_probe(
        Probe("agent_grounding_history0", "agent", "grounding", 0), config, runtime, private,
    )
    assert len(transport.requests) == len(result["requests"]) == 2
    assert result["tool_calls"] == result["successful_tools"] == ["KB_search"]
    assert result["passed"] is False
    assert "RETRIEVAL_ANSWER_NOT_GROUNDED" in result["issues"]
    assert "RETRIEVAL_EVIDENCE_MISSING" not in result["issues"]


def assert_partial_trace(result, private, error_code, *, messages_available=True):
    assert result["passed"] is False
    assert result["trace_partial"] is True
    assert error_code in result["issues"]
    raw = (private / (result["probe_id"] + ".json")).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result["trace_sha256"]
    trace = json.loads(raw)
    assert trace["error_code"] == error_code and trace["partial"] is True
    assert trace["messages_available"] is result["trace_messages_available"] is messages_available
    assert trace["messages_source"] == "recorded_orchestrator_trajectory"
    assert "AUTHORED_EXCEPTION_TEXT_MUST_NOT_BE_REPORTED" not in raw.decode()
    assert all("raw_data" not in message for message in (trace["messages"] or []))
    assert "reward_info" not in trace and "reward_info" not in result
    return trace


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_completed_read_survives_later_one_attempt_budget_failure(runtime_fixture, monkeypatch, cleanup_error):
    from safe_agent.benchmarks.tau3.adapter import SafeTauAgent

    config, private, create = runtime_fixture
    if cleanup_error:
        stop = SafeTauAgent.stop
        cleanups = []

        def fail_after_second_cleanup(agent, *args, **kwargs):
            stop(agent, *args, **kwargs)
            cleanups.append(True)
            if len(cleanups) == 2:
                raise RuntimeError("AUTHORED_EXCEPTION_TEXT_MUST_NOT_BE_REPORTED")

        monkeypatch.setattr(SafeTauAgent, "stop", fail_after_second_cleanup)
    runtime, transport = create([
        json.dumps({"kind": "tool", "tool_name": "KB_search",
                    "arguments": {"query": "user priority lower monthly fees"}}),
        json.dumps({"kind": "message", "purpose": "answer", "text": "Must never be requested."}),
    ], episode_budget={"max_model_calls": 1}, run_budget={"max_model_calls": 1})
    result = _run_agent_probe(
        Probe("agent_retained_preference_history0", "agent", "retained_preference", 0),
        config, runtime, private,
    )
    trace = assert_partial_trace(result, private, "AGENT_PROBE_EXECUTION_FAILED")
    if cleanup_error:
        assert cleanups == [True, True]
        assert trace["cleanup_error_code"] == "AGENT_PROBE_CLEANUP_FAILED"
    else:
        assert "cleanup_error_code" not in trace
    assert result["issues"] == ["AGENT_PROBE_EXECUTION_FAILED"]
    assert len(transport.requests) == len(result["requests"]) == 1
    request, reply, status = runtime.private.inference(result["requests"][0]["request_id"])
    assert status == result["requests"][0]["status"] == "COMPLETE"
    assert reply is not None and reply.usage_source == "scripted"
    assert result["requests"][0]["input_tokens"] == reply.input_tokens
    assert result["requests"][0]["output_tokens"] == reply.output_tokens
    assert request.episode_id == result["episode_id"]
    assert result["tool_calls"] == result["successful_tools"] == ["KB_search"]
    assert [message["role"] for message in trace["messages"]] == ["assistant", "tool"]
    call = trace["messages"][0]["tool_calls"][0]
    observed = trace["messages"][1]
    assert call["arguments"] == {"query": "user priority lower monthly fees"}
    assert observed["id"] == call["id"] and observed["error"] is False
    assert observed["content"]
    assert runtime.episodes.get_call(result["episode_id"], call["id"]).success is True
    assert runtime.episodes.inspect(result["episode_id"]).usage.model_calls == 1
    assert runtime.episodes.inspect(result["episode_id"]).usage.tool_calls == 1
    assert result["response_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("phase", ["proposal", "result"])
@pytest.mark.parametrize("exception,error_code", [
    (RuntimeError, "AGENT_PROBE_EXECUTION_FAILED"), (KeyboardInterrupt, "QUALIFICATION_INTERRUPTED"),
])
def test_interrupted_trajectory_retains_only_recorded_messages_without_replay_or_receipt(
    runtime_fixture, monkeypatch, phase, exception, error_code,
):
    from tau2.orchestrator.orchestrator import Orchestrator

    from safe_agent.contracts.episodes import EpisodeState

    config, private, create = runtime_fixture
    runtime, transport = create([
        json.dumps({"kind": "tool", "tool_name": "KB_search",
                    "arguments": {"query": "user priority lower monthly fees"}}),
        json.dumps({"kind": "message", "purpose": "answer", "text": "Must never be requested."}),
    ])
    step = Orchestrator.step

    def interrupted_step(orchestrator):
        step(orchestrator)
        last = orchestrator.get_messages()[-1]
        if (phase == "proposal" and last.role == "assistant" and last.is_tool_call()
                or phase == "result" and last.role == "tool"):
            raise exception("AUTHORED_EXCEPTION_TEXT_MUST_NOT_BE_REPORTED")

    monkeypatch.setattr(Orchestrator, "step", interrupted_step)
    result = _run_agent_probe(
        Probe("agent_retained_preference_history0", "agent", "retained_preference", 0),
        config, runtime, private,
    )
    trace = assert_partial_trace(result, private, error_code)
    assert result["issues"] == [error_code]
    assert len(transport.requests) == len(result["requests"]) == 1
    assert result["requests"][0]["status"] == "COMPLETE"
    assert result["tool_calls"] == ["KB_search"]
    assert result["successful_tools"] == (["KB_search"] if phase == "result" else [])
    assert [message["role"] for message in trace["messages"]] == (
        ["assistant", "tool"] if phase == "result" else ["assistant"]
    )
    call = trace["messages"][0]["tool_calls"][0]
    view = runtime.episodes.inspect(result["episode_id"])
    # Interruption before the next agent step leaves any observed result
    # unconsumed by the host. Trace preservation cannot mint a host receipt.
    assert view.state is EpisodeState.OUTCOME_UNCERTAIN
    assert view.pending_call_id == call["id"]
    assert runtime.episodes.get_call(result["episode_id"], call["id"]).success is None
    assert runtime.private.load(result["episode_id"]).data["successful_calls"] == {}
    if phase == "result":
        assert trace["messages"][1]["id"] == call["id"]
        assert trace["messages"][1]["error"] is False


def test_unavailable_partial_trajectory_is_unknown_not_an_empty_tool_history(
    runtime_fixture, monkeypatch,
):
    from tau2.orchestrator.orchestrator import Orchestrator

    config, private, create = runtime_fixture
    runtime, transport = create([
        json.dumps({"kind": "tool", "tool_name": "KB_search",
                    "arguments": {"query": "user priority lower monthly fees"}}),
    ], episode_budget={"max_model_calls": 1}, run_budget={"max_model_calls": 1})

    def unavailable(_):
        raise RuntimeError("AUTHORED_EXCEPTION_TEXT_MUST_NOT_BE_REPORTED")

    monkeypatch.setattr(Orchestrator, "get_messages", unavailable)
    result = _run_agent_probe(
        Probe("agent_retained_preference_history0", "agent", "retained_preference", 0),
        config, runtime, private,
    )
    trace = assert_partial_trace(result, private, "AGENT_PROBE_EXECUTION_FAILED",
                                 messages_available=False)
    assert trace["messages"] is None
    assert trace["trace_error_code"] == "AGENT_PROBE_TRAJECTORY_UNAVAILABLE"
    assert result["tool_calls"] is result["successful_tools"] is result["response_sha256"] is None
    assert len(transport.requests) == len(result["requests"]) == 1
    assert result["requests"][0]["status"] == "COMPLETE"
    assert runtime.episodes.inspect(result["episode_id"]).usage.tool_calls == 1
