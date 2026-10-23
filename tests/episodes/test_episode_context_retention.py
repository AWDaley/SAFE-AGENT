"""Deterministic bounded prompts over authored history and public τ tool results."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import create_engine

from safe_agent.benchmarks.tau3.protocol import Message, Observation, RuntimeContext, ToolSpec
from safe_agent.contracts.episode_inference import wire_digest, wire_json
from safe_agent.conversation.context_evidence import (
    ContextRetentionDenied,
    ObservedResult,
    collect_evidence,
)
from safe_agent.domains import ToolProposal, get_domain_pack
from safe_agent.inference.episode_gateway import EpisodeInferenceDenied, ScriptedEpisodeTransport
from safe_agent.orchestration.episode_runtime import EpisodeRuntime
from tests.episodes.test_episode_foundation import NOW

admit_domain_action = pytest.importorskip("tal.domain_admission").admit_domain_action
INNER = "get_debit_dispute_status_7483"


def native_search(documents, *, timing=True):
    result = "\n".join(
        f"{rank}. {title}\n   ID: {identifier}\n   Score: 1.0000\n   Content: {content}\n"
        for rank, (identifier, title, content) in enumerate(documents, 1)
    )
    return result + ("\n\n[Timing: retrieval=0.01ms, total=0.02ms]" if timing else "")


@pytest.mark.parametrize("timing", [False, True])
def test_literal_spans_keep_exact_native_unicode_offsets_and_old_unlock_source(timing):
    content = "Authored policy π🙂.\n" + "Background paragraph.\n" * 1600
    content += f"\nThe operation is {INNER}; request the scoped user_id.\n"
    native = native_search([
        ("doc_authored_first", "Authored policy", content),
        ("doc_authored_second", "Other observed policy", "Other content.\n"),
    ], timing=timing)
    source = ObservedResult("search_old", "KB_search", {"query": "scoped user_id"}, native, True, 2)
    definition = f"Tool unlocked: {INNER}\nParameters:\n  - user_id: string (required)\n"
    unlock = ObservedResult("unlock_old", "unlock_discoverable_agent_tool",
                            {"agent_tool_name": INNER}, definition, True, 4)
    newest = ObservedResult("search_new", "KB_search", {"query": "recent policy"}, native_search([
        ("doc_authored_new", "Recent policy", "A recent observed policy."),
    ]), True, 100)
    result = collect_evidence([source, unlock, newest], unlocked_tools=(INNER,),
                              discovered_tools=(INNER,))
    old = next(doc for doc in result.required_documents if doc["document_id"] == "doc_authored_first")
    assert old["content_hash"] == wire_digest(content)
    assert native[old["result_start"]:old["result_end"]] == content
    assert old["source"]["result_hash"] == wire_digest(native)
    assert INNER in old["required_for"]
    assert any(span["start"] > 20_000 and INNER in span["text"] for span in old["spans"])
    for span in old["spans"]:
        assert content[span["start"]:span["end"]] == span["text"]
        assert wire_digest(span["text"]) == span["text_hash"]
    assert result.unlocked_definitions[0]["definition_text"] == definition
    assert result.unlocked_definitions[0]["discovery_source"] == {
        key: old[key] for key in ("document_id", "content_hash", "source", "result_start", "result_end")
    }
    assert result.inventory["observed_document_occurrences"] == 3
    assert len(result.optional_documents) == 1
    assert result == collect_evidence([source, unlock, newest], unlocked_tools=(INNER,),
                                      discovered_tools=(INNER,))


@pytest.mark.parametrize("case", ["no_unlock", "failed_unlock", "missing_discovery", "failed_discovery"])
def test_unlocked_definition_requires_successful_correlated_observed_sources(case):
    search = ObservedResult("search", "KB_search", {"query": "status"},
                             "Observed documentation for " + INNER, case != "failed_discovery", 1)
    unlock = ObservedResult("unlock", "unlock_discoverable_agent_tool", {"agent_tool_name": INNER},
                             f"Tool unlocked: {INNER}\nNative parameters", case != "failed_unlock", 3)
    results = ([search] if case == "no_unlock" else [unlock] if case == "missing_discovery"
               else [search, unlock])
    with pytest.raises(ContextRetentionDenied, match="EVIDENCE_MISSING"):
        collect_evidence(results, unlocked_tools=(INNER,), discovered_tools=(INNER,))


def runtime_fixture(tmp_path, domain, *, limit=32768, propose=None):
    transport = ScriptedEpisodeTransport(propose or (lambda _: json.dumps({
        "kind": "message", "purpose": "question", "text": "What would you like to do next?",
    })))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'retention.sqlite'}")
    EpisodeRuntime.create_fixture_schema(engine)
    runtime = EpisodeRuntime(
        RuntimeContext(run_id="retention_fixture", domain=domain, model="fixture-model",
                       episode_budget={"max_prompt_bytes": limit}, output_dir=str(tmp_path)),
        engine=engine, transport=transport, admission_evaluator=admit_domain_action, clock=lambda: NOW,
    )
    return runtime, transport


def customer(text, identifier="obs_current"):
    return Observation(observation_id=identifier, messages=[Message(role="user", content=text)])


def dialogue(count=16):
    return [Message(role=role, content=f"Earlier discussion {index} {role}. " + "ordinary words " * 400)
            for index in range(count) for role in ("user", "assistant")]


def test_actual_banking_bm25_unlock_and_subject_survive_long_history(monkeypatch, tmp_path):
    """Native retrieval/definitions; entirely authored customer/history, no official task."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    import litellm
    from tau2.domains.banking_knowledge.data_model import DatabaseTable, TransactionalDB
    from tau2.domains.banking_knowledge.environment import get_environment

    from safe_agent.benchmarks.tau3.adapter import tool_specs

    def no_provider(*args, **kwargs):
        raise AssertionError("The retention fixture must not call a hosted provider")

    for name in ("completion", "acompletion", "embedding"):
        monkeypatch.setattr(litellm, name, no_provider)
    user = {"user_id": "authored_retention_customer", "name": "Ava Example",
            "email": "ava.retention@example.test", "phone_number": "415-555-0199",
            "address": "1 Example Way", "date_of_birth": "02/03/1980"}
    environment = get_environment(
        db=TransactionalDB(users=DatabaseTable(data={user["user_id"]: user})), retrieval_variant="bm25",
    )
    history = [Message(role="user", content=(
        "I am Ava Example. My email is ava.retention@example.test and phone is 415-555-0199. "
        "I want to check debit-card dispute statuses."
    ))]
    native_results = {}

    def execute_prior(name, arguments):
        call_id = f"prior_{len(native_results)}"
        result = getattr(environment.tools, name)(**arguments)
        native_results[name] = result
        history.extend([
            Message(role="assistant", call_id=call_id, tool_name=name, arguments=arguments),
            Message(role="tool", call_id=call_id, content=result),
        ])
        return result

    execute_prior("get_user_information_by_name", {"customer_name": user["name"]})
    now = execute_prior("get_current_time", {})
    execute_prior("log_verification", {**user, "time_verified": now.removeprefix("The current time is ")[:-1]})
    execute_prior("KB_search", {"query": "Checking Debit Card Dispute Status"})
    execute_prior("unlock_discoverable_agent_tool", {"agent_tool_name": INNER})
    history.extend(dialogue())

    def propose(request):
        projected = request.projected_context
        if projected["successful_calls"]:
            return json.dumps({"kind": "message", "purpose": "answer", "text": "The lookup has returned."})
        assert projected["subject"]["authenticated"]
        return json.dumps({"kind": "tool", "tool_name": "call_discoverable_agent_tool", "arguments": {
            "agent_tool_name": INNER, "arguments": json.dumps({"user_id": projected["subject"]["user_id"]}),
        }})

    runtime, transport = runtime_fixture(tmp_path, "banking_knowledge", propose=propose)
    state = runtime.start_episode(episode_id="ep_retention", domain="banking_knowledge",
                                  policy=environment.get_policy(), tools=tool_specs(environment.get_tools()),
                                  history=history)
    decision, state = runtime.step(customer("Please check the dispute history now."), state)
    assert decision.kind == "tool" and decision.tool_name == "call_discoverable_agent_tool"
    projected = transport.requests[0].projected_context
    assert len(wire_json(projected).encode("utf-8")) <= 32768
    assert projected["omitted_history_messages"] > 0
    assert not any(item.get("tool_name") == "KB_search" for item in projected["conversation"])
    definition = next(item for item in projected["unlocked_tool_definitions"] if item["name"] == INNER)
    assert definition["definition_text"] == native_results["unlock_discoverable_agent_tool"]
    document = next(doc for doc in projected["retrieval_documents"] if INNER in doc["required_for"])
    search = native_results["KB_search"]
    assert document["source"]["result_hash"] == wire_digest(search)
    for span in document["spans"]:
        native_span = search[document["result_start"] + span["start"]:document["result_start"] + span["end"]]
        assert span["text_hash"] == wire_digest(native_span)
    assert projected["subject"]["user_id"].startswith("[[safe:user_id:")
    assert any(isinstance(value, dict) and "email" in value for value in projected["observed_facts"].values())
    encoded = wire_json(projected)
    for private in user.values():
        assert private not in encoded
    assert {item["name"] for item in projected["unlocked_tool_definitions"]} == {INNER}
    assert all(name in search for name in projected["discovered_tools"])
    assert len(projected["tool_schemas"]) == 15
    assert INNER not in {tool["name"] for tool in projected["tool_schemas"]}
    stored, _, _ = runtime.private.inference(transport.requests[0].request_id)
    assert stored.projected_context == projected
    # Reprojection after restoring the same private state is deterministic,
    # including reference reuse and the pending call's durable authorization.
    data = runtime.private.load("ep_retention").data
    first = runtime._project(data, runtime._domain_context(data), None)
    second = runtime._project(deepcopy(data), runtime._domain_context(data), None)
    assert first == second
    assert first["pending_action"]["call_id"] == decision.call_id
    assert first["pending_action"]["domain_admission_hash"]
    # The native tool executes once outside SAFE, then its result becomes the
    # sole success receipt; pre-existing calls did not mint SAFE receipts.
    result = environment.tools.call_discoverable_agent_tool(**decision.arguments)
    runtime.step(Observation(observation_id="obs_result", messages=[
        Message(role="tool", call_id=decision.call_id, content=result),
    ]), state)
    assert runtime.episodes.inspect("ep_retention").usage.tool_calls == 1
    assert len(transport.requests[-1].projected_context["successful_calls"]) == 1


def test_exact_pending_confirmation_and_authorized_action_survive_history_eviction(tmp_path):
    user = {"user_id": "retention_customer", "orders": [], "payment_methods": {},
            "address": {"address1": "1 Old Street", "zip": "78702"}}
    history = [
        Message(role="user", content="My email is person.retention@example.test."),
        Message(role="assistant", call_id="lookup", tool_name="find_user_id_by_email",
                arguments={"email": "person.retention@example.test"}),
        Message(role="tool", call_id="lookup", content=user["user_id"]),
        Message(role="assistant", call_id="details", tool_name="get_user_details",
                arguments={"user_id": user["user_id"]}),
        Message(role="tool", call_id="details", content=json.dumps(user)),
        *dialogue(),
    ]

    def propose(request):
        return json.dumps({"kind": "tool", "tool_name": "modify_user_address", "arguments": {
            "user_id": request.projected_context["subject"]["user_id"],
            "address1": "123 New Street", "address2": "", "city": "Austin", "state": "TX",
            "country": "USA", "zip": "78701",
        }})

    runtime, transport = runtime_fixture(tmp_path, "retail", propose=propose)
    tools = [ToolSpec(name=cap.name, parameters=cap.parameters)
             for cap in get_domain_pack("retail", admission_evaluator=admit_domain_action).capabilities.values()]
    state = runtime.start_episode(episode_id="ep_retention", domain="retail", policy="Authored retail policy",
                                  tools=tools, history=history)
    confirmation, state = runtime.step(customer("Change my address to 123 New Street, ZIP 78701."), state)
    assert confirmation.kind == "message" and "confirm" in confirmation.content.lower()
    data = runtime.private.load("ep_retention").data
    proposal = ToolProposal.model_validate_json(wire_json(data["pending_consent"]["proposal"]))
    projected = runtime._project(data, runtime._domain_context(data), None)
    binding = projected["pending_confirmation"]
    assert binding["proposal_hash"] == proposal.digest
    assert binding["arguments_hash"] == proposal.arguments_hash
    assert binding["displayed_text_hash"] == wire_digest(confirmation.content)
    assert binding["customer"] == {"user_id": projected["subject"]["user_id"]}
    assert projected["consent_binding"] is None
    assert projected["omitted_history_messages"] > 0
    assert "retention_customer" not in wire_json(projected)
    call, state = runtime.step(customer("Yes.", "obs_confirm"), state)
    assert call.kind == "tool" and len(transport.requests) == 1
    data = runtime.private.load("ep_retention").data
    projected = runtime._project(data, runtime._domain_context(data), None)
    assert projected["pending_confirmation"] is None
    action = projected["pending_action"]
    assert action["proposal_hash"] == proposal.digest
    assert action["arguments_hash"] == proposal.arguments_hash
    assert action["call_id"] == call.call_id
    assert action["authorization_hash"] == call.authorization_id
    assert action["consent_hash"] == runtime.episodes.get_call("ep_retention", call.call_id).call.consent_hash
    assert action["consent_hash"] and projected["consent_binding"] is None
    assert len(wire_json(projected).encode("utf-8")) <= 32768


@pytest.mark.parametrize("oversized", ["policy", "customer", "schema", "unlocked_definition"])
def test_irreducible_core_rejects_before_model_reservation(tmp_path, oversized):
    domain = "banking_knowledge" if oversized == "unlocked_definition" else "retail"
    runtime, transport = runtime_fixture(tmp_path, domain)
    # Below the independent customer-intake 64 KiB ceiling, above this run's
    # prompt ceiling. The required material itself must never be prefix-cut.
    giant = "Irreducible content. " * 2500
    tools = [ToolSpec(name="get_user_details", description=giant if oversized == "schema" else "",
                      parameters={"type": "object", "properties": {"user_id": {"type": "string"}},
                                  "required": ["user_id"]})]
    history = [Message(role="user", content="Initial customer intent.")]
    if oversized == "unlocked_definition":
        tools = [ToolSpec(name=name, parameters={"type": "object", "properties": {
            field: {"type": "string"}}, "required": [field]})
            for name, field in (("KB_search", "query"), ("unlock_discoverable_agent_tool", "agent_tool_name"))]
        history.extend([
            Message(role="assistant", tool_name="KB_search", call_id="prior_search", arguments={"query": "status"}),
            Message(role="tool", call_id="prior_search", content="Authored documentation for " + INNER),
            Message(role="assistant", tool_name="unlock_discoverable_agent_tool", call_id="prior_unlock",
                    arguments={"agent_tool_name": INNER}),
            Message(role="tool", call_id="prior_unlock", content=f"Tool unlocked: {INNER}\n" + giant),
        ])
    state = runtime.start_episode(episode_id="ep_retention", domain=domain,
                                  policy=giant if oversized == "policy" else "Fixture policy", tools=tools,
                                  history=history)
    with pytest.raises(EpisodeInferenceDenied, match="EPISODE_REQUIRED_CONTEXT_TOO_LARGE"):
        runtime.step(customer(giant if oversized == "customer" else "Please continue."), state)
    assert not transport.requests
    view = runtime.episodes.inspect("ep_retention")
    assert view.usage.model_calls == 0 and view.usage.cost_microusd == 0

