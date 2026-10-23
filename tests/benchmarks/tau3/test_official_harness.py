"""Authored, deterministic compatibility probes; these are not benchmark scores."""

import pytest

from safe_agent.benchmarks.tau3.adapter import official_agent_class
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.protocol import Decision
from safe_agent.benchmarks.tau3.runner import register_safe_agent, text_run_config


def _upstream(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm

    def no_model(*args, **kwargs):
        raise AssertionError("Offline harness compatibility tests must not invoke a model")

    monkeypatch.setattr(litellm, "completion", no_model)
    monkeypatch.setattr(litellm, "acompletion", no_model)


class ScriptedRuntime:
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


def test_same_process_registration_discards_privileged_task(monkeypatch, tmp_path):
    _upstream(monkeypatch)
    from tau2.agent.base_agent import HalfDuplexAgent
    from tau2.domains.retail.environment import get_environment
    from tau2.registry import registry

    contexts = []
    runtime = ScriptedRuntime([])

    def runtime_factory(context):
        contexts.append(context)
        return runtime

    class HiddenTask:
        def __getattribute__(self, name):
            raise AssertionError("The adapter attempted to inspect privileged task metadata")

    config = BenchmarkConfig(domain="retail", run_id="registration_probe")
    name = register_safe_agent(config, output_dir=tmp_path, runtime_factory=runtime_factory)
    try:
        environment = get_environment()
        agent = registry.get_agent_factory(name)(
            tools=environment.get_tools(),
            domain_policy=environment.get_policy(),
            task=HiddenTask(),
            llm="fixture-model",
            llm_args={"seed": 42},
        )
        assert isinstance(agent, HalfDuplexAgent)
        state = agent.get_init_state()
        assert state.episode_id.startswith("ep_")
        assert contexts[0].model == "fixture-model"
        assert contexts[0].model_args == {"seed": 42}
        assert not hasattr(contexts[0], "task")
        assert registry.get_agent_task_filter(name) is None
        official = text_run_config(config, agent_name=name, output_dir=tmp_path)
        assert official.agent == name
        assert official.max_retries == 0
        assert not official.auto_review and not official.auto_resume
        assert official.log_level == "INFO"
        assert not official.verbose_logs
    finally:
        registry._agent_factories.pop(name, None)
        registry._agent_metadata.pop(name, None)


def test_authored_episode_runs_real_tools_and_unchanged_all_evaluator_once(monkeypatch):
    _upstream(monkeypatch)
    from tau2.data_model.message import UserMessage
    from tau2.data_model.tasks import Action, EvaluationCriteria, RewardType, Task, UserScenario
    from tau2.domains.retail.environment import get_environment
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.runner.simulation import run_simulation
    from tau2.user.user_simulator_base import HalfDuplexUser

    environment = get_environment()
    user_id, record = next(iter(environment.tools.db.users.items()))
    new_address = {
        "user_id": user_id,
        "address1": "101 Authored Fixture Road",
        "address2": "",
        "city": "Boston",
        "state": "MA",
        "country": "USA",
        "zip": "02110",
    }
    decisions = [
        Decision(
            kind="tool",
            tool_name="find_user_id_by_email",
            arguments={"email": record.email},
            call_id="fixture_lookup",
            authorization_id="fixture_auth_lookup",
        ),
        Decision(
            kind="message",
            content="Confirm your new default address: 101 Authored Fixture Road, Boston, MA 02110, USA?",
        ),
        Decision(
            kind="tool",
            tool_name="modify_user_address",
            arguments=new_address,
            call_id="fixture_write",
            authorization_id="fixture_auth_write",
        ),
        Decision(kind="message", content="Your default address was updated.", stop=True),
    ]
    runtime = ScriptedRuntime(decisions)
    agent = official_agent_class()(
        tools=environment.get_tools(),
        domain_policy=environment.get_policy(),
        domain="retail",
        runtime=runtime,
    )

    class ScriptedUser(HalfDuplexUser):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            content = (
                f"My email is {record.email}; change my default address to 101 Authored Fixture Road, Boston, MA 02110, USA."
                if state == 0
                else "Yes, please make that change."
            )
            return UserMessage(role="user", content=content, cost=0.0), state + 1

        def set_seed(self, seed):
            pass

    task = Task(
        id="authored_adapter_fixture",
        user_scenario=UserScenario(instructions="Authored offline compatibility fixture"),
        evaluation_criteria=EvaluationCriteria(
            actions=[
                Action(
                    action_id="expected_address", name="modify_user_address", arguments=new_address
                )
            ],
            reward_basis=[RewardType.DB],
        ),
    )
    emitted = []
    original = environment.get_response

    def execute_once(message):
        emitted.append(message.name)
        return original(message)

    monkeypatch.setattr(environment, "get_response", execute_once)
    orchestrator = Orchestrator(
        domain="retail",
        agent=agent,
        user=ScriptedUser(),
        environment=environment,
        task=task,
        max_steps=20,
        max_errors=2,
        seed=42,
        validate_communication=True,
    )
    outcome = run_simulation(orchestrator, evaluation_type=EvaluationType.ALL)
    assert outcome.reward_info.reward == 1.0
    assert outcome.termination_reason.value == "agent_stop"
    assert emitted == ["find_user_id_by_email", "modify_user_address"]
    assert environment.tools.db.users[user_id].address.address1 == new_address["address1"]
    writes = [
        message
        for message in outcome.messages
        if getattr(message, "tool_calls", None)
        and message.tool_calls[0].name == "modify_user_address"
    ]
    assert len(writes) == 1
    assert writes[0].raw_data["safe_authorization_id"] == "fixture_auth_write"
