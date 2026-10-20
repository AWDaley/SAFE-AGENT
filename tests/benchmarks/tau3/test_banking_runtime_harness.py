"""Real BM25, discovery and SAFE episode gates using an authored offline case."""

import json
from collections import Counter

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


def test_banking_user_tool_grant_keeps_execution_with_official_user(monkeypatch, tmp_path):
    """Grant -> instructions -> official user action -> authoritative read.

    The customer/database/task and inference choices are authored. Public BM25
    documents and both toolkits are real; this is not a published-task score.
    """
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm
    from tal.domain_admission import admit_domain_action
    from tau2.data_model.message import ToolCall, UserMessage
    from tau2.data_model.tasks import Task, UserScenario
    from tau2.domains.banking_knowledge.data_model import DatabaseTable, TransactionalDB
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.user.user_simulator_base import HalfDuplexUser

    from safe_agent.contracts.episodes import EpisodeState
    from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
    from safe_agent.orchestration.episode_runtime import EpisodeRuntime

    def no_model(*args, **kwargs):
        raise AssertionError("This authored fixture must not invoke a model or embeddings")

    for name in ("completion", "acompletion", "embedding"):
        monkeypatch.setattr(litellm, name, no_model)
    customer = {
        "user_id": "authored_referral_customer", "name": "Ava Example",
        "email": "ava@example.test", "address": "1 Example Way",
        "phone_number": "415-555-0199", "date_of_birth": "02/03/1980",
    }
    card_name = "Platinum Rewards Card"
    db = TransactionalDB(users=DatabaseTable(data={customer["user_id"]: customer}))
    environment = get_environment(db=db, retrieval_variant="bm25")
    inner = "get_referral_link"
    bad_claim_attempted = False
    instructions_sent = False
    answer_sent = False

    def propose(request):
        nonlocal bad_claim_attempted, instructions_sent, answer_sent
        context = request.projected_context
        receipts = context["successful_calls"]
        completed = Counter(entry["tool_name"] for entry in receipts.values())
        conversation = context["conversation"]

        def tool(name, arguments):
            return {"kind": "tool", "tool_name": name, "arguments": arguments}

        user = next((value for value in context["observed_facts"].values()
                     if isinstance(value, dict) and "email" in value and "user_id" in value), None)
        if not completed["get_user_information_by_name"]:
            assert inner not in json.dumps(context)
            value = tool("get_user_information_by_name", {"customer_name": "Ava Example"})
        elif not completed["get_current_time"]:
            value = tool("get_current_time", {})
        elif not completed["log_verification"]:
            assert user is not None
            value = tool("log_verification", {
                **{key: user[key] for key in customer},
                "time_verified": context["observed_facts"]["bank:current_time"],
            })
        elif completed["KB_search"] == 0:
            value = tool("KB_search", {"query": "Generating a Credit Card Referral Link"})
        elif completed["KB_search"] == 1:
            assert inner in json.dumps(conversation)
            value = tool("KB_search", {"query": "referral $100 $1,500 within 90 days"})
        elif not completed["get_referrals_by_user"]:
            assert "doc_credit_cards_platinum_rewards_card_008" in json.dumps(conversation)
            value = tool("get_referrals_by_user", {"user_id": user["user_id"]})
        elif not completed["give_discoverable_user_tool"]:
            assert context["authenticated"]
            value = tool("give_discoverable_user_tool", {
                "discoverable_tool_name": inner,
                "arguments": json.dumps({"user_id": user["user_id"], "card_name": card_name}),
            })
        elif not bad_claim_attempted:
            # A grant receipt must not become proof of the user's business action.
            bad_claim_attempted = True
            grant_id = next(key for key, receipt in receipts.items()
                            if receipt["tool_name"] == "give_discoverable_user_tool")
            value = {"kind": "message", "purpose": "success", "text": "Referral completed.",
                     "evidence_call_ids": [grant_id]}
        elif not instructions_sent:
            assert context["feedback"] == "SUCCESS_REQUIRES_MUTATION_RESULT"
            instructions_sent = True
            value = {"kind": "message", "purpose": "user_instruction", "text": (
                f"Please run {inner} yourself with user_id={user['user_id']} and "
                f"card_name='{card_name}', then tell me what it returns. The bonus is $100 "
                "after approval and $1,500 spend within 90 days, with at most seven bonuses "
                "per year and two in any rolling seven days."
            )}
        elif completed["get_referrals_by_user"] == 1:
            assert any(item["role"] == "user" and "generated the link" in item["content"]
                       for item in conversation)
            value = tool("get_referrals_by_user", {"user_id": user["user_id"]})
        elif not answer_sent:
            observed = next(item for item in reversed(conversation)
                            if item["role"] == "tool"
                            and item["tool_name"] == "get_referrals_by_user")
            assert "NO_PROGRESS" in observed["content"]
            answer_sent = True
            value = {"kind": "message", "purpose": "answer", "text": (
                "Your referral record shows NO_PROGRESS. The referred person has not applied yet."
            )}
        else:
            value = {"kind": "message", "purpose": "finish", "text": "Goodbye.", "stop": True}
        return json.dumps(value)

    transport = ScriptedEpisodeTransport(propose)
    engine = create_engine("sqlite://")
    EpisodeRuntime.create_fixture_schema(engine)
    runtime = EpisodeRuntime(
        RuntimeContext(run_id="bank_user_action_fixture", domain="banking_knowledge",
                       model="fixture-model", output_dir=str(tmp_path)),
        engine=engine, transport=transport, admission_evaluator=admit_domain_action,
    )
    agent = official_agent_class()(
        tools=environment.get_tools(), domain_policy=environment.get_policy(),
        domain="banking_knowledge", runtime=runtime, episode_id="ep_bank_user_action",
    )
    assert inner not in {spec.name for spec in agent.specs}
    assert "call_discoverable_user_tool" not in {spec.name for spec in agent.specs}

    class ScriptedUser(HalfDuplexUser):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            if state == 0:
                text = (
                    "I am Ava Example. My email is ava@example.test and phone is 415-555-0199. "
                    "Help me generate my first Platinum Rewards Card referral link. I understand "
                    "the bonus is $100 after approval and $1,500 spend within 90 days, up to seven "
                    "per year and two in a rolling week."
                )
            elif state == 1:
                assert inner in message.content and customer["user_id"] in message.content
                assert card_name in message.content
                assert len(environment.tools.db.user_discoverable_tools.data) == 1
                assert not environment.tools.db.user_discoverable_tool_calls.data
                assert not environment.tools.db.referrals.data
                # The official orchestrator routes this user request to the
                # official user toolkit. SAFE never executes this capability.
                return UserMessage(role="user", cost=0.0, tool_calls=[ToolCall(
                    id="authored_user_action", name="call_discoverable_user_tool", requestor="user",
                    arguments={"discoverable_tool_name": inner, "arguments": json.dumps({
                        "user_id": customer["user_id"], "card_name": card_name,
                    })},
                )]), state + 1
            elif state == 2:
                assert message.requestor == "user" and message.id == "authored_user_action"
                assert "Referral link generated successfully." in message.content
                text = "I generated the link. Please check that it appears in my referral history."
            else:
                assert "NO_PROGRESS" in message.content
                text = "Thank you."
            return UserMessage(role="user", content=text, cost=0.0), state + 1

        def set_seed(self, seed):
            pass

    calls = []
    original = environment.get_response

    def execute(message):
        calls.append((message.requestor, message.name))
        return original(message)

    monkeypatch.setattr(environment, "get_response", execute)
    outcome = Orchestrator(
        domain="banking_knowledge", agent=agent,
        user=ScriptedUser(tools=environment.get_user_tools()), environment=environment,
        task=Task(id="authored_bank_user_action", user_scenario=UserScenario(
            instructions="Authored referral grant and user-only execution fixture",
        )), max_steps=40, max_errors=2, seed=42, validate_communication=True,
    ).run()
    assert outcome.termination_reason.value == "agent_stop"
    assert calls == [
        ("assistant", "get_user_information_by_name"), ("assistant", "get_current_time"),
        ("assistant", "log_verification"), ("assistant", "KB_search"),
        ("assistant", "KB_search"), ("assistant", "get_referrals_by_user"),
        ("assistant", "give_discoverable_user_tool"), ("user", "call_discoverable_user_tool"),
        ("assistant", "get_referrals_by_user"),
    ]
    assert len(environment.tools.db.referrals.data) == 1
    assert len(environment.tools.db.user_discoverable_tool_calls.data) == 1
    assert not environment.tools.db.agent_discoverable_tools.data
    private = runtime.private.load("ep_bank_user_action").data
    assert any(item["code"] == "SUCCESS_REQUIRES_MUTATION_RESULT"
               for item in private["diagnostics"])
    assert all(receipt["tool_name"] != inner for receipt in private["successful_calls"].values())
    grant = next(receipt for receipt in private["successful_calls"].values()
                 if receipt["tool_name"] == "give_discoverable_user_tool")
    assert grant["effect"] == "control"
    view = runtime.episodes.inspect("ep_bank_user_action")
    assert view.state == EpisodeState.FINISHED and view.usage.tool_calls == 8
    for request in transport.requests:
        serialized = request.model_dump_json()
        assert customer["email"] not in serialized
        assert customer["user_id"] not in serialized
        assert "evaluation_criteria" not in serialized
        assert all(item.get("tool_name") != "call_discoverable_user_tool"
                   for item in request.projected_context["conversation"])
