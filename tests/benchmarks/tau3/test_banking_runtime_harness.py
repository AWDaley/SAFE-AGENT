"""Real BM25, discovery and SAFE episode gates using an authored offline case."""

import json

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.adapter import official_agent_class
from safe_agent.benchmarks.tau3.protocol import RuntimeContext


def test_banking_bm25_discovery_unlock_with_actual_safe_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm
    from tal.domain_admission import admit_domain_action
    from tau2.data_model.message import UserMessage
    from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.runner.simulation import run_simulation
    from tau2.user.user_simulator_base import HalfDuplexUser

    from safe_agent.contracts.episodes import EpisodeState
    from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
    from safe_agent.orchestration.episode_runtime import EpisodeRuntime

    def no_model(*args, **kwargs):
        raise AssertionError("This fixture must not invoke a model or embeddings")

    monkeypatch.setattr(litellm, "completion", no_model)
    monkeypatch.setattr(litellm, "acompletion", no_model)
    monkeypatch.setattr(litellm, "embedding", no_model)
    environment = get_environment(retrieval_variant="bm25")
    inner_name = "get_debit_dispute_status_7483"
    answer_sent = False

    def propose(request):
        nonlocal answer_sent
        context = request.projected_context
        receipts = context["successful_calls"]
        completed = {entry["tool_name"] for entry in receipts.values()}
        if "KB_search" not in completed:
            assert inner_name not in json.dumps(context)
            value = {
                "kind": "tool",
                "tool_name": "KB_search",
                "arguments": {
                    "query": "Checking Debit Card Dispute Status",
                },
            }
        elif "unlock_discoverable_agent_tool" not in completed:
            search = next(
                item
                for item in reversed(context["conversation"])
                if item["role"] == "tool" and item["tool_name"] == "KB_search"
            )
            assert inner_name in json.dumps(search["content"])
            value = {
                "kind": "tool",
                "tool_name": "unlock_discoverable_agent_tool",
                "arguments": {"agent_tool_name": inner_name},
            }
        elif not answer_sent:
            answer_sent = True
            value = {
                "kind": "message",
                "purpose": "policy",
                "text": (
                    "The dispute history can show open, provisional-credit, and resolved statuses. "
                    "Before checking your own dispute history I need to verify your identity."
                ),
            }
        else:
            value = {"kind": "message", "purpose": "finish", "text": "Goodbye.", "stop": True}
        return json.dumps(value)

    transport = ScriptedEpisodeTransport(propose)
    engine = create_engine("sqlite://")
    EpisodeRuntime.create_fixture_schema(engine)
    runtime = EpisodeRuntime(
        RuntimeContext(
            run_id="banking_runtime_fixture",
            domain="banking_knowledge",
            model="fixture-model",
            output_dir=str(tmp_path),
        ),
        engine=engine,
        transport=transport,
        admission_evaluator=admit_domain_action,
    )
    agent = official_agent_class()(
        tools=environment.get_tools(),
        domain_policy=environment.get_policy(),
        domain="banking_knowledge",
        runtime=runtime,
        episode_id="ep_banking_runtime_fixture",
    )
    assert len(agent.specs) == 15
    assert inner_name not in {spec.name for spec in agent.specs}

    class ScriptedUser(HalfDuplexUser):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            text = (
                "Can you explain debit-card dispute statuses and whether you can check the history?"
                if state == 0
                else "Thank you, that is all for now."
            )
            return UserMessage(role="user", content=text, cost=0.0), state + 1

        def set_seed(self, seed):
            pass

    task = Task(
        id="authored_banking_discovery_fixture",
        user_scenario=UserScenario(instructions="Authored public-KB discovery fixture"),
        evaluation_criteria=EvaluationCriteria(actions=[], reward_basis=[RewardType.DB]),
    )
    calls = []
    original = environment.get_response

    def execute(message):
        calls.append(message.name)
        return original(message)

    monkeypatch.setattr(environment, "get_response", execute)
    outcome = run_simulation(
        Orchestrator(
            domain="banking_knowledge",
            agent=agent,
            user=ScriptedUser(),
            environment=environment,
            task=task,
            max_steps=20,
            max_errors=2,
            seed=42,
            validate_communication=True,
        ),
        evaluation_type=EvaluationType.ALL,
        env_kwargs={"retrieval_variant": "bm25"},
    )
    assert outcome.termination_reason.value == "agent_stop"
    assert outcome.reward_info.reward == 1.0
    assert calls == ["KB_search", "unlock_discoverable_agent_tool"]
    private = runtime.private.load("ep_banking_runtime_fixture").data
    assert inner_name in private["domain_context"]["discovered_tools"]
    assert inner_name in private["domain_context"]["unlocked_tools"]
    assert not private["domain_context"]["authenticated"]
    assert runtime.episodes.inspect("ep_banking_runtime_fixture").state == EpisodeState.FINISHED
    for request in transport.requests:
        visible_names = {spec["name"] for spec in request.projected_context["tool_schemas"]}
        assert visible_names == {spec.name for spec in agent.specs}
        assert "required_documents" not in request.model_dump_json()
