"""Concrete run, consent, result and recovery boundaries without a model API."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.protocol import Message, Observation, RuntimeContext, ToolSpec
from safe_agent.contracts.episodes import EpisodeState
from safe_agent.domains import get_domain_pack
from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
from safe_agent.orchestration.episode_runtime import EpisodeRuntime
from safe_agent.storage.episode_runtime import RuntimeStorageDenied
from tests.episodes.test_episode_foundation import NOW

admit_domain_action = pytest.importorskip("tal.domain_admission").admit_domain_action


def runtime_fixture(tmp_path, propose=None, *, run_id="runtime_audit", model_args=None):
    transport = ScriptedEpisodeTransport(propose or (lambda _: json.dumps({
        "kind": "message", "purpose": "question", "text": "What would you like to do?",
    })))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'runtime.sqlite'}")
    EpisodeRuntime.create_fixture_schema(engine)
    context = RuntimeContext(run_id=run_id, domain="retail", model="fixture-model",
                             model_args=model_args or {}, output_dir=str(tmp_path))
    runtime = EpisodeRuntime(context, engine=engine, transport=transport,
                             admission_evaluator=admit_domain_action, clock=lambda: NOW)
    pack = get_domain_pack("retail", admission_evaluator=admit_domain_action)
    tools = [ToolSpec(name=cap.name, parameters=cap.parameters)
             for cap in pack.capabilities.values()]
    return runtime, transport, context, tools, engine


def customer(text, identifier):
    return Observation(observation_id=identifier, messages=[Message(role="user", content=text)])


def start(runtime, tools, *, history=()):
    return runtime.start_episode(episode_id="ep_audit", domain="retail", policy="Fixture policy",
                                 tools=tools, history=list(history))


def address_fixture(tmp_path):
    def propose(request):
        facts = request.projected_context["observed_facts"]
        user = next(value for value in facts.values() if isinstance(value, dict) and "user_id" in value)
        return json.dumps({"kind": "tool", "tool_name": "modify_user_address", "arguments": {
            "user_id": user["user_id"], "address1": "123 New Street", "address2": "",
            "city": "Austin", "state": "TX", "country": "USA", "zip": "78701",
        }})

    runtime, transport, _, tools, engine = runtime_fixture(tmp_path, propose)
    user = {"user_id": "customer_1", "orders": [], "payment_methods": {},
            "address": {"address1": "1 Old Street", "zip": "78702"}}
    history = [
        Message(role="user", content="My email is person@example.test."),
        Message(role="assistant", call_id="prior_lookup", tool_name="find_user_id_by_email",
                arguments={"email": "person@example.test"}),
        Message(role="tool", call_id="prior_lookup", tool_name="find_user_id_by_email", content="customer_1"),
        Message(role="assistant", call_id="prior_user", tool_name="get_user_details",
                arguments={"user_id": "customer_1"}),
        Message(role="tool", call_id="prior_user", tool_name="get_user_details", content=json.dumps(user)),
    ]
    state = start(runtime, tools, history=history)
    message, state = runtime.step(customer("Change my address to 123 New Street, ZIP 78701.", "obs_1"), state)
    assert message.kind == "message" and "confirm" in message.content.lower()
    return runtime, transport, state, user, engine


def test_one_affirmation_authorizes_only_one_outgoing_call(tmp_path):
    runtime, transport, state, user, _ = address_fixture(tmp_path)
    call, state = runtime.step(customer("Yes.", "obs_2"), state)
    assert call.kind == "tool" and len(transport.requests) == 1
    receipt = runtime.episodes.get_call("ep_audit", call.call_id)
    assert receipt.call.consent_hash is not None
    assert receipt.authorization.domain_admission_hash is not None
    assert runtime.private.load("ep_audit").data["domain_context"]["consent"] is None
    # An otherwise valid second update with identical arguments must display a
    # new confirmation. The first customer's yes cannot survive the result.
    message, _ = runtime.step(Observation(observation_id="obs_3", messages=[
        Message(role="tool", call_id=call.call_id, content=json.dumps(user)),
    ]), state)
    assert message.kind == "message" and "confirm" in message.content.lower()
    assert runtime.episodes.inspect("ep_audit").usage.tool_calls == 1
    assert runtime.episodes.get_call("ep_audit", call.call_id).success is True


@pytest.mark.parametrize("earlier", ["No.", "Use a different address."])
def test_multi_message_batch_cannot_skip_changed_or_negative_consent(tmp_path, earlier):
    runtime, _, state, _, _ = address_fixture(tmp_path)
    decision, _ = runtime.step(Observation(observation_id="obs_2", messages=[
        Message(role="user", content=earlier), Message(role="user", content="Yes."),
    ]), state)
    assert decision.kind == "message" and "confirm" in decision.content.lower()
    assert runtime.episodes.inspect("ep_audit").usage.tool_calls == 0


@pytest.mark.parametrize("result", ["  Error: update rejected", "\nFailed: update rejected"])
def test_failed_native_result_cannot_mint_success_receipt(tmp_path, result):
    runtime, _, state, _, _ = address_fixture(tmp_path)
    call, state = runtime.step(customer("Yes.", "obs_2"), state)
    runtime.step(Observation(observation_id="obs_3", messages=[
        Message(role="tool", call_id=call.call_id, content=result),
    ]), state)
    assert runtime.episodes.get_call("ep_audit", call.call_id).success is False
    assert runtime.private.load("ep_audit").data["successful_calls"] == {}


def test_runtime_cannot_step_or_close_another_run_episode(tmp_path):
    first, first_transport, _, tools, engine = runtime_fixture(tmp_path)
    state = start(first, tools)
    other_transport = ScriptedEpisodeTransport(lambda _: pytest.fail("Foreign run dispatched inference"))
    other = EpisodeRuntime(RuntimeContext(run_id="other_run", domain="retail", model="fixture-model",
                                           output_dir=str(tmp_path)), engine=engine,
                            transport=other_transport, admission_evaluator=admit_domain_action,
                            clock=lambda: NOW)
    with pytest.raises(RuntimeStorageDenied, match="RUN_SCOPE"):
        other.step(customer("Hello", "obs_1"), state)
    with pytest.raises(RuntimeStorageDenied, match="RUN_SCOPE"):
        other.close_episode(state)
    assert first.private.load("ep_audit").active_observation is None
    first.step(customer("Hello", "obs_1"), state)
    assert len(first_transport.requests) == 1


@pytest.mark.parametrize("field", ["policy", "tools", "deadline", "domain_context"])
def test_mutable_private_context_cannot_replace_frozen_episode_sources(tmp_path, field):
    runtime, transport, _, tools, _ = runtime_fixture(tmp_path)
    state = start(runtime, tools)
    claim = runtime.private.claim("ep_audit", "host_tamper_fixture", 0)
    data = claim.data
    if field == "policy":
        data["policy"] = "Replacement policy"
    elif field == "tools":
        data["tools"][0]["description"] = "Replacement tool metadata"
    elif field == "deadline":
        data["deadline"] = (NOW + timedelta(days=2)).isoformat()
    else:
        data["domain_context"]["episode_id"] = "foreign_episode"
    runtime.private.save("ep_audit", claim, data)
    state["version"] = 2
    with pytest.raises(RuntimeStorageDenied, match="SOURCE_BINDING_CHANGED"):
        runtime.step(customer("Hello", "obs_1"), state)
    assert runtime.private.load("ep_audit").active_observation is None
    assert runtime.episodes.inspect("ep_audit").usage.model_calls == 0
    assert not transport.requests


def test_caller_mutating_context_dict_does_not_change_frozen_model_configuration(tmp_path):
    runtime, transport, context, tools, _ = runtime_fixture(tmp_path, model_args={"temperature": 0.25})
    context.model_args["temperature"] = 1.75
    runtime.step(customer("Hello", "obs_1"), start(runtime, tools))
    assert transport.requests[0].temperature == 0.25


def test_lost_context_save_never_reemits_prepared_write(tmp_path, monkeypatch):
    runtime, transport, state, _, engine = address_fixture(tmp_path)

    def save_failure(*args, **kwargs):
        raise RuntimeStorageDenied("FIXTURE_SAVE_INTERRUPTED")

    monkeypatch.setattr(runtime.private, "save", save_failure)
    with pytest.raises(RuntimeStorageDenied, match="SAVE_INTERRUPTED"):
        runtime.step(customer("Yes.", "obs_2"), state)
    pending = runtime.episodes.inspect("ep_audit")
    assert pending.state == EpisodeState.TOOL_PENDING and pending.usage.tool_calls == 1
    assert runtime.episodes.get_call("ep_audit", pending.pending_call_id).emit_allowed is False
    restarted = EpisodeRuntime(runtime.context, engine=engine, transport=transport,
                                admission_evaluator=admit_domain_action, clock=lambda: NOW)
    with pytest.raises(RuntimeStorageDenied):
        restarted.step(customer("Yes.", "obs_2"), state)
    restarted.close_episode(state)
    assert restarted.episodes.inspect("ep_audit").state == EpisodeState.OUTCOME_UNCERTAIN
    assert len(transport.requests) == 1


@pytest.mark.parametrize("version", [False, 0.0, "0", -1])
def test_runtime_state_version_is_a_strict_nonnegative_integer(tmp_path, version):
    runtime, _, _, tools, _ = runtime_fixture(tmp_path)
    state = start(runtime, tools)
    state["version"] = version
    with pytest.raises(RuntimeStorageDenied, match="INVALID_RUNTIME_STATE"):
        runtime.step(customer("Hello", "obs_1"), state)
