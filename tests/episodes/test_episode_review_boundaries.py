"""Regression checks at the actual model-request and completion boundaries."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.protocol import Message, Observation, RuntimeContext, ToolSpec
from safe_agent.contracts.episode_inference import wire_json
from safe_agent.conversation.intake import ProjectionDenied, ReferenceVault
from safe_agent.domains import get_domain_pack
from safe_agent.inference.episode_gateway import ScriptedEpisodeTransport
from safe_agent.orchestration.episode_runtime import EpisodeRuntime
from tests.episodes.test_episode_foundation import NOW

admit_domain_action = pytest.importorskip("tal.domain_admission").admit_domain_action


def has_literal(text, value):
    # Four-digit canaries can coincidentally occur inside random reference IDs
    # or content hashes. Test actual scalar/text occurrences, not hash substrings.
    return re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", text) is not None


def vault_for(*, customer_texts=()):
    return ReferenceVault(
        episode_id="ep_review", actor_id="customer", deadline=NOW + timedelta(hours=1),
        tools=[{"name": "call_discoverable_agent_tool"}, {"name": "get_user_details"}],
        customer_texts=customer_texts,
    )


@pytest.mark.parametrize("field,value,kind", [
    ("pin", "0862", "pin"), ("current_pin", "0862", "pin"),
    ("new_pin", "7924", "pin"), ("cvv", "573", "cvv"),
])
def test_structured_secret_fields_are_opaque_and_only_resolve_as_tool_arguments(field, value, kind):
    vault = vault_for()
    projected = vault.project({"notes": f"The supplied code is {value}.", field: value,
                               "amount": 45.75, "year": 2026, "count": 3},
                              source="customer", subject_id="user_1")
    token = projected[field]
    assert token.startswith(f"[[safe:{kind}:")
    assert not has_literal(wire_json(projected), value)
    assert (projected["amount"], projected["year"], projected["count"]) == (45.75, 2026, 3)
    native = vault.resolve_arguments("call_discoverable_agent_tool", {
        "agent_tool_name": "authored_secret_tool", "arguments": wire_json({field: token}),
    }, now=NOW, subject_id="user_1")
    assert json.loads(native["arguments"]) == {field: value}
    for text in (token, f"Your code is {value}."):
        with pytest.raises(ProjectionDenied, match="SECRET_DISCLOSURE"):
            vault.render(text, now=NOW, subject_id="user_1", authenticated=True)
    with pytest.raises(ProjectionDenied, match="SCOPE_DENIED"):
        vault.resolve_arguments("get_user_details", {field: token},
                                now=NOW, subject_id="other_user")


@pytest.mark.parametrize("text,values", [
    ("My current PIN is 0862 and my CVV is 573.", ("0862", "573")),
    ('{"current_pin":"0862","new_pin":"7924","cvv":"573"}', ("0862", "7924", "573")),
    ("Set my new PIN to 7924. CVV: 573", ("7924", "573")),
])
def test_labelled_customer_secrets_become_source_bound_references(text, values):
    vault = vault_for(customer_texts=(text,))
    projected = vault.project_text(text, source="customer", subject_id="user_1")
    for value in values:
        assert not has_literal(projected, value)
        record = next(record for record in vault.records if record["value"] == value)
        assert record["source"] == "customer" and record["subject_id"] == "user_1"
    # The public scalar fallback must not permit a model to guess a credential,
    # even when the same digits occur in a host-owned customer message.
    for field, value in (("current_pin", values[0]), ("cvv", values[-1])):
        with pytest.raises(ProjectionDenied, match="REQUIRES_REFERENCE"):
            vault.resolve_arguments("get_user_details", {field: value}, now=NOW, subject_id="user_1")


def test_numeric_amounts_and_identifiers_are_not_guessed_to_be_secrets():
    vault = vault_for()
    ordinary = "Transfer 7924 dollars in 2026. There are 573 items; order X0862Y costs $86.20."
    assert vault.project_text(ordinary, source="customer", subject_id="user_1") == ordinary
    vault.project({"current_pin": "0862", "cvv": "573"}, source="customer", subject_id="user_1")
    unaffected = "Order X0862Y and count 1573 cost $57.30."
    assert vault.project_text(unaffected, source="tool", subject_id="user_1") == unaffected
    assert vault.render(unaffected, now=NOW, subject_id="user_1", authenticated=True) == unaffected
    with pytest.raises(ProjectionDenied, match="SECRET_DISCLOSURE"):
        vault.render("Your new PIN is 9348.", now=NOW, subject_id="user_1", authenticated=True)


def banking_runtime(tmp_path, propose, *, context=None, initial_history=(), model_args=None):
    pack = get_domain_pack("banking_knowledge", admission_evaluator=admit_domain_action)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'review.sqlite'}")
    EpisodeRuntime.create_fixture_schema(engine)
    transport = ScriptedEpisodeTransport(propose)
    instance = EpisodeRuntime(
        RuntimeContext(run_id="review", domain="banking_knowledge", model="fixture-model",
                       model_args=model_args or {},
                       output_dir=str(tmp_path)),
        engine=engine, transport=transport, admission_evaluator=admit_domain_action,
        clock=lambda: NOW,
    )
    history = list(initial_history)
    if context is not None:
        history.append(Message(role="user", content=context.user_messages[0]))
        for index, observed in enumerate(context.observations):
            call_id = f"prior_{index}"
            history.extend([
                Message(role="assistant", call_id=call_id, tool_name=observed.tool_name,
                        arguments=observed.arguments),
                Message(role="tool", call_id=call_id, tool_name=observed.tool_name,
                        content=observed.result if isinstance(observed.result, str)
                        else json.dumps(observed.result)),
            ])
    state = instance.start_episode(
        episode_id="ep_review", domain="banking_knowledge", policy="Authored fixture",
        tools=[ToolSpec(name=cap.name, parameters=cap.parameters)
               for cap in pack.capabilities.values()], history=history,
    )
    return instance, transport, state


def user(text, identifier="obs_customer"):
    return Observation(observation_id=identifier, messages=[Message(role="user", content=text)])


def test_runtime_accepts_exact_retry_and_output_cap_aliases(tmp_path):
    instance, transport, state = banking_runtime(tmp_path, lambda _: json.dumps({
        "kind": "message", "purpose": "question", "text": "How can I help you?",
    }), model_args={"temperature": 0.0, "num_retries": 0, "max_tokens": 2048})
    instance.step(user("Hello."), state)
    assert len(transport.requests) == 1 and transport.requests[0].max_output_tokens == 2048


@pytest.mark.parametrize("arguments", [
    {"num_retries": 1}, {"num_retries": False}, {"max_tokens": 2049}, {"max_tokens": 2048.0},
])
def test_runtime_rejects_model_arguments_that_change_retry_or_output_authority(tmp_path, arguments):
    with pytest.raises(ValueError):
        banking_runtime(tmp_path, lambda _: pytest.fail("Rejected configuration invoked a model"),
                        model_args=arguments)


@pytest.mark.parametrize("profile", ["PRIVACY_PROFILE", "COMPLETION_PROFILE"])
def test_changed_profile_cannot_reuse_an_existing_run(tmp_path, monkeypatch, profile):
    import safe_agent.orchestration.episode_runtime as runtime_module
    from safe_agent.storage.episode_runtime import RuntimeStorageDenied

    current = getattr(runtime_module, profile)
    monkeypatch.setattr(runtime_module, profile, "previous-authored-profile")
    instance, transport, _ = banking_runtime(tmp_path, lambda _: pytest.fail("Unexpected inference"))
    monkeypatch.setattr(runtime_module, profile, current)
    with pytest.raises(RuntimeStorageDenied, match="RUN_CONFIGURATION_CHANGED"):
        EpisodeRuntime(instance.context, engine=instance.episodes.engine, transport=transport,
                       admission_evaluator=admit_domain_action, clock=lambda: NOW)
    assert not transport.requests


def test_pin_and_cvv_do_not_reach_actual_episode_model_request(tmp_path):
    instance, transport, state = banking_runtime(tmp_path, lambda _: json.dumps({
        "kind": "message", "purpose": "question", "text": "How can I help you?",
    }))
    native = "My current PIN is 0862 and my CVV is 573. My order total is 45.75."
    instance.step(user(native), state)
    request = transport.requests[0]
    encoded = request.model_dump_json()
    assert not has_literal(encoded, "0862") and not has_literal(encoded, "573")
    customer = next(message["content"] for message in request.projected_context["conversation"]
                    if message["role"] == "user")
    assert "[[safe:pin:" in customer and "[[safe:cvv:" in customer
    assert "45.75" in customer
    assert request.projected_context["privacy_profile"] == "synthetic_benchmark_field_policy_v2"


@pytest.mark.parametrize("prompt,kind", [
    ("Please provide your current PIN.", "pin"),
    ("What is your CVV?", "cvv"),
    ("What are your current PIN and CVV?", "unclassified_payment_secret"),
])
def test_bare_numeric_secret_reply_is_opaque_at_runtime_and_ambiguity_cannot_authorize_a_kind(tmp_path, prompt, kind):
    instance, transport, state = banking_runtime(tmp_path, lambda _: json.dumps({
        "kind": "message", "purpose": "question", "text": "How can I help you?",
    }), initial_history=[Message(role="assistant", content=prompt)])
    instance.step(user("0862"), state)
    request = transport.requests[0]
    customer = next(message["content"] for message in request.projected_context["conversation"]
                    if message["role"] == "user")
    assert customer.startswith(f"[[safe:{kind}:")
    assert not has_literal(request.model_dump_json(), "0862")
    if kind == "unclassified_payment_secret":
        vault = instance._vault(instance.private.load("ep_review").data)
        for field in ("pin", "cvv"):
            with pytest.raises(ProjectionDenied, match="SECRET_TYPE_REQUIRED"):
                vault.resolve_arguments("call_discoverable_agent_tool", {
                    "agent_tool_name": "authored_secret_tool", "arguments": wire_json({field: customer}),
                }, now=NOW, subject_id=None)


@pytest.mark.parametrize("mode", ["reference", "literal"])
def test_runtime_blocks_secret_echoes_without_putting_rejected_text_in_next_prompt(tmp_path, mode):
    def propose(request):
        projected = request.projected_context
        if projected["feedback"] is None:
            customer = next(message["content"] for message in projected["conversation"]
                            if message["role"] == "user")
            token = re.search(r"\[\[safe:pin:[a-f0-9]{32}\]\]", customer)[0]
            value = token if mode == "reference" else "0862"
            return json.dumps({"kind": "message", "purpose": "answer", "text": f"Your PIN is {value}."})
        assert projected["feedback"] == "PRIVATE_SECRET_DISCLOSURE_DENIED"
        return json.dumps({"kind": "message", "purpose": "question", "text": "How can I help you?"})

    instance, transport, state = banking_runtime(tmp_path, propose)
    response, _ = instance.step(user("My current PIN is 0862."), state)
    assert response.content == "How can I help you?" and len(transport.requests) == 2
    assert all(not has_literal(request.model_dump_json(), "0862") for request in transport.requests)


def test_read_wrapper_result_cannot_support_mutation_completion(tmp_path):
    from tests.domains.test_domain_packs import banking_fixture, discover

    pack, context, _ = banking_fixture()
    inner = "get_all_user_accounts_by_user_id_3847"
    context = discover(pack, context, inner)

    def propose(request):
        projected = request.projected_context
        receipts = projected["successful_calls"]
        if not receipts:
            customer = next(value for value in projected["observed_facts"].values()
                            if isinstance(value, dict) and "email" in value and "user_id" in value)
            return json.dumps({"kind": "tool", "tool_name": "call_discoverable_agent_tool",
                               "arguments": {"agent_tool_name": inner,
                                             "arguments": json.dumps({"user_id": customer["user_id"]})}})
        if projected["feedback"] is None:
            return json.dumps({"kind": "message", "purpose": "success", "text": "all done",
                               "evidence_call_ids": [next(iter(receipts))]})
        assert projected["feedback"] == "SUCCESS_REQUIRES_MUTATION_RESULT"
        return json.dumps({"kind": "message", "purpose": "answer", "text": "I found no accounts."})

    instance, transport, state = banking_runtime(tmp_path, propose, context=context)
    call, state = instance.step(user("List my accounts."), state)
    response, _ = instance.step(Observation(observation_id="obs_result", messages=[
        Message(role="tool", call_id=call.call_id, tool_name=call.tool_name,
                content="Found 0 record(s) in 'accounts':"),
    ]), state)
    receipt = instance.private.load("ep_review").data["successful_calls"][call.call_id]
    assert receipt["dispatch_effect"] == "write" and receipt["effect"] == "read"
    assert response.content == "I found no accounts." and len(transport.requests) == 3
    assert instance.episodes.get_call("ep_review", call.call_id).emit_allowed is False


def test_secret_references_resolve_at_dispatch_and_remain_private_on_readback(tmp_path):
    from tests.domains.test_domain_packs import bank_record, banking_fixture, discover, observe

    pack, context, _ = banking_fixture()
    for inner, args, result in (
        ("get_all_user_accounts_by_user_id_3847", {"user_id": "user_1"},
         bank_record("accounts", [("acct_fixture", {"account_id": "acct_fixture", "user_id": "user_1"})])),
        ("get_debit_cards_by_account_id_7823", {"account_id": "acct_fixture"},
         [{"card_id": "card_fixture", "account_id": "acct_fixture", "pin": "0862"}]),
    ):
        context = discover(pack, context, inner)
        context = observe(pack, context, "call_discoverable_agent_tool", result,
                          agent_tool_name=inner, arguments=json.dumps(args))
    inner = "change_debit_card_pin_6285"
    context = discover(pack, context, inner)

    def propose(request):
        projected = request.projected_context
        if projected["successful_calls"]:
            return json.dumps({"kind": "message", "purpose": "success", "text": "all done",
                               "evidence_call_ids": [next(iter(projected["successful_calls"]))]})
        latest = next(message["content"] for message in reversed(projected["conversation"])
                      if message["role"] == "user")
        current_pin = re.search(r"current PIN is (\[\[safe:pin:[a-f0-9]{32}\]\])", latest)[1]
        new_pin = re.search(r"new PIN to (\[\[safe:pin:[a-f0-9]{32}\]\])", latest)[1]
        card = next(value for value in projected["observed_facts"].values()
                    if isinstance(value, dict) and "card_id" in value)
        return json.dumps({"kind": "tool", "tool_name": "call_discoverable_agent_tool",
                           "arguments": {"agent_tool_name": inner, "arguments": json.dumps({
                               "card_id": card["card_id"], "current_pin": current_pin, "new_pin": new_pin,
                           })}})

    instance, transport, state = banking_runtime(tmp_path, propose, context=context)
    call, state = instance.step(user("My current PIN is 0862. Set my new PIN to 7924."), state)
    assert json.loads(call.arguments["arguments"]) == {
        "card_id": "card_fixture", "current_pin": "0862", "new_pin": "7924",
    }
    response, _ = instance.step(Observation(observation_id="obs_result", messages=[
        Message(role="tool", call_id=call.call_id, tool_name=call.tool_name,
                content="PIN changed successfully. New PIN: 7924"),
    ]), state)
    for request in transport.requests:
        assert not has_literal(request.model_dump_json(), "0862")
        assert not has_literal(request.model_dump_json(), "7924")
    assert response.content == "The tool confirmed the requested action: change debit card pin 6285."
    receipt = instance.private.load("ep_review").data["successful_calls"][call.call_id]
    assert receipt["effect"] == "business_write" and receipt["dispatch_effect"] == "write"
