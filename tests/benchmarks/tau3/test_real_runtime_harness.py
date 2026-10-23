"""Real SAFE policy/journal/gateway with scripted inference and real τ tools.

This is an authored offline integration fixture, not a published-task score.
The local TAL evaluator is injected explicitly for tests; the production factory
continues to require its independently reviewed installed TAL revision.
"""

import json
import re

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.adapter import official_agent_class
from safe_agent.benchmarks.tau3.diagnostics import diagnose_database
from safe_agent.benchmarks.tau3.protocol import RuntimeContext


def test_real_safe_runtime_cancels_owned_order_after_customer_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm
    from tal.domain_admission import admit_domain_action
    from tau2.data_model.message import UserMessage
    from tau2.data_model.tasks import Action, EvaluationCriteria, RewardType, Task, UserScenario
    from tau2.domains.retail.environment import get_environment
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.runner.simulation import run_simulation
    from tau2.user.user_simulator_base import HalfDuplexUser

    from safe_agent.contracts.episodes import EpisodeState
    from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
    from safe_agent.orchestration.episode_runtime import EpisodeRuntime

    def no_model(*args, **kwargs):
        raise AssertionError("No provider calls are permitted in this fixture")

    monkeypatch.setattr(litellm, "completion", no_model)
    monkeypatch.setattr(litellm, "acompletion", no_model)
    environment = get_environment()
    order_id, order = next(
        (key, value)
        for key, value in environment.tools.db.orders.items()
        if value.status == "pending"
    )
    customer = environment.tools.db.users[order.user_id]
    order_index = customer.orders.index(order_id)
    fixture_responses = []

    def propose(request):
        context = request.projected_context
        conversation = context["conversation"]
        receipts = context["successful_calls"]
        completed = {entry["tool_name"] for entry in receipts.values()}
        if "find_user_id_by_email" not in completed:
            text = next(item["content"] for item in conversation if item["role"] == "user")
            email_reference = re.search(r"\[\[safe:email:[a-f0-9]{32}\]\]", text).group()
            value = {
                "kind": "tool",
                "tool_name": "find_user_id_by_email",
                "arguments": {"email": email_reference},
            }
        elif "get_user_details" not in completed:
            lookup = next(
                item
                for item in reversed(conversation)
                if item["role"] == "tool" and item["tool_name"] == "find_user_id_by_email"
            )
            value = {
                "kind": "tool",
                "tool_name": "get_user_details",
                "arguments": {"user_id": lookup["content"]["user_id"]},
            }
        elif "get_order_details" not in completed:
            details = next(
                item
                for item in reversed(conversation)
                if item["role"] == "tool" and item["tool_name"] == "get_user_details"
            )
            value = {
                "kind": "tool",
                "tool_name": "get_order_details",
                "arguments": {"order_id": details["content"]["orders"][order_index]},
            }
        elif "cancel_pending_order" not in completed:
            details = next(
                item
                for item in reversed(conversation)
                if item["role"] == "tool" and item["tool_name"] == "get_order_details"
            )
            value = {
                "kind": "tool",
                "tool_name": "cancel_pending_order",
                "arguments": {
                    "order_id": details["content"]["order_id"],
                    "reason": "no longer needed",
                },
            }
        elif not fixture_responses:
            write_id = next(
                key
                for key, entry in receipts.items()
                if entry["tool_name"] == "cancel_pending_order"
            )
            value = {
                "kind": "message",
                "purpose": "success",
                "text": "Outcome summary",
                "evidence_call_ids": [write_id],
            }
            fixture_responses.append("summary")
        else:
            value = {"kind": "message", "purpose": "finish", "text": "Goodbye.", "stop": True}
        return json.dumps(value)

    transport = ScriptedEpisodeTransport(propose)
    database = tmp_path / "retail_runtime.sqlite"
    engine = create_engine("sqlite+pysqlite:///" + str(database))
    EpisodeRuntime.create_fixture_schema(engine)
    runtime = EpisodeRuntime(
        RuntimeContext(
            run_id="real_runtime_fixture",
            domain="retail",
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
        domain="retail",
        runtime=runtime,
        episode_id="ep_real_runtime_fixture",
    )

    class ScriptedUser(HalfDuplexUser):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            if state == 0:
                text = (
                    f"My email is {customer.email}. Cancel order {order_id}; I no longer need it."
                )
            elif state == 1:
                assert "confirm" in message.content.lower()
                assert environment.tools.db.orders[order_id].status == "pending"
                text = "Yes."
            else:
                text = "Thank you."
            return UserMessage(role="user", content=text, cost=0.0), state + 1

        def set_seed(self, seed):
            pass

    task = Task(
        id="authored_real_runtime_fixture",
        user_scenario=UserScenario(instructions="Authored consent/cancellation fixture"),
        evaluation_criteria=EvaluationCriteria(
            actions=[
                Action(
                    action_id="expected_cancellation",
                    name="cancel_pending_order",
                    arguments={"order_id": order_id, "reason": "no longer needed"},
                )
            ],
            reward_basis=[RewardType.DB],
        ),
    )
    calls = []
    original = environment.get_response

    def execute(message):
        calls.append(message.name)
        return original(message)

    monkeypatch.setattr(environment, "get_response", execute)
    orchestrator = Orchestrator(
        domain="retail",
        agent=agent,
        user=ScriptedUser(),
        environment=environment,
        task=task,
        max_steps=30,
        max_errors=2,
        seed=42,
        validate_communication=True,
    )
    try:
        outcome = run_simulation(orchestrator, evaluation_type=EvaluationType.ALL)
    except Exception as exc:
        conversation = (
            transport.requests[-1].projected_context["conversation"] if transport.requests else []
        )
        raise AssertionError(
            f"Fixture failed with {type(exc).__name__}; projected conversation={conversation!r}"
        ) from exc
    assert outcome.reward_info.reward == 1.0
    assert outcome.termination_reason.value == "agent_stop"
    assert calls == [
        "find_user_id_by_email",
        "get_user_details",
        "get_order_details",
        "cancel_pending_order",
    ]
    view = runtime.episodes.inspect("ep_real_runtime_fixture")
    assert view.state == EpisodeState.FINISHED
    assert view.usage.tool_calls == 4
    assert view.usage.model_calls == len(transport.requests)
    private = runtime.private.load("ep_real_runtime_fixture").data
    writes = {
        key: value
        for key, value in private["successful_calls"].items()
        if value["effect"] == "business_write"
    }
    assert len(writes) == 1
    call_id = next(iter(writes))
    receipt = runtime.episodes.get_call("ep_real_runtime_fixture", call_id)
    assert receipt.success is True
    assert receipt.call.consent_hash is not None
    diagnostics = diagnose_database(database)
    assert diagnostics["integrity"]["passed"]
    assert diagnostics["episodes"]["by_state"] == {"FINISHED": 1}
    assert diagnostics["authorization"]["bindings_with_domain_admission_receipt"] == 4
    assert diagnostics["authorization"]["bindings_with_customer_consent"] == 1
    assert diagnostics["denials"]["by_reason"] == {"CUSTOMER_CONSENT_REQUIRED": 1}
    assert diagnostics["model_attempts"]["known_replies"] == len(transport.requests)
    for request in transport.requests:
        serialized = request.model_dump_json()
        assert customer.email not in serialized
        assert "required_documents" not in serialized
        assert "evaluation_criteria" not in serialized
