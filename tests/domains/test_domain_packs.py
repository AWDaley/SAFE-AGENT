"""Authored boundary fixtures; no official tasks, databases, or model calls."""

import json

import pytest
from pydantic import ValidationError

from safe_agent.domains import CustomerConsent, DomainContext, ToolProposal, get_domain_pack
from safe_agent.domains.schema import ArgumentSchemaError, parse_argument_object, validate_arguments

admit_domain_action = pytest.importorskip(
    "tal.domain_admission",
    reason="Benchmark domain TAL revision is not installed in office-only CI",
).admit_domain_action

POLICY = "sha256:" + "1" * 64
OBSERVATION = "sha256:" + "2" * 64


def pack_for(domain):
    return get_domain_pack(domain, admission_evaluator=admit_domain_action)


def context_for(domain):
    return DomainContext(
        episode_id="episode1",
        domain=domain,
        policy_hash=POLICY,
        observation_hash=OBSERVATION,
        state_version=0,
    )


def proposal(domain, tool, **args):
    return ToolProposal(domain=domain, tool_name=tool, arguments=args)


def observe(pack, context, tool, result, **args):
    p = proposal(pack.name, tool, **args)
    assert pack.admit(p, context).allowed
    return pack.observe(p, result, context)


def retail_fixture():
    pack = pack_for("retail")
    ctx = pack.observe_user(
        "My email is ava@example.test", context_for("retail"), user_turn_id="u1"
    )
    ctx = observe(pack, ctx, "find_user_id_by_email", "ava_1", email="ava@example.test")
    ctx = observe(
        pack,
        ctx,
        "get_user_details",
        {
            "user_id": "ava_1",
            "orders": ["#W1"],
            "payment_methods": {
                "credit1": {"source": "credit_card", "id": "credit1"},
                "credit2": {"source": "credit_card", "id": "credit2"},
                "gift1": {"source": "gift_card", "id": "gift1", "balance": 30.0},
            },
        },
        user_id="ava_1",
    )
    ctx = observe(
        pack,
        ctx,
        "get_order_details",
        {
            "order_id": "#W1",
            "user_id": "ava_1",
            "status": "pending",
            "items": [{"item_id": "old1", "product_id": "prod1", "price": 10.0}],
            "payment_history": [
                {"transaction_type": "payment", "payment_method_id": "credit1", "amount": 10.0}
            ],
        },
        order_id="#W1",
    )
    return pack, ctx


def consent_for(p, ctx):
    return CustomerConsent(
        episode_id=ctx.episode_id,
        proposal_digest=p.digest,
        subject_id=ctx.subject_id,
        user_turn_id=ctx.latest_user_turn_id,
        policy_hash=ctx.policy_hash,
    )


def bank_record(table, records):
    text = f"Found {len(records)} record(s) in '{table}':\n\n"
    for index, (record_id, fields) in enumerate(records, 1):
        text += f"{index}. Record ID: {record_id}\n"
        text += "".join(f"   {name}: {value}\n" for name, value in fields.items()) + "\n"
    return text


def banking_fixture(authenticated=True):
    pack = pack_for("banking_knowledge")
    ctx = pack.observe_user(
        "I am Ava Lewis. My email is ava@example.test and phone number is 555-0101.",
        context_for("banking_knowledge"),
        user_turn_id="u1",
    )
    fields = {
        "name": "Ava Lewis",
        "user_id": "user_1",
        "address": "1 Main St",
        "email": "ava@example.test",
        "phone_number": "555-0101",
        "date_of_birth": "01/02/1980",
    }
    ctx = observe(
        pack,
        ctx,
        "get_user_information_by_name",
        bank_record("users", [("user_1", fields)]),
        customer_name="Ava Lewis",
    )
    assert ctx.subject_id == "user_1" and not ctx.authenticated
    ctx = observe(pack, ctx, "get_current_time", "The current time is 2025-11-14 03:40:00 EST.")
    verification = proposal(
        "banking_knowledge", "log_verification", **fields, time_verified="2025-11-14 03:40:00 EST"
    )
    if authenticated:
        assert pack.admit(verification, ctx).allowed
        ctx = pack.observe(
            verification, "Verification logged successfully.\n  - User: Ava Lewis", ctx
        )
        assert ctx.authenticated
    return pack, ctx, verification


def discover(pack, ctx, name):
    ctx = observe(pack, ctx, "KB_search", f"To resolve this issue use {name}.", query="policy")
    ctx = observe(
        pack,
        ctx,
        "unlock_discoverable_agent_tool",
        f"Tool unlocked: {name}\nDescription: fixture",
        agent_tool_name=name,
    )
    return ctx


def test_retail_requires_observed_lookup_before_private_read():
    pack = pack_for("retail")
    ctx = pack.observe_user("My user ID is ava_1", context_for("retail"), user_turn_id="u1")
    decision = pack.admit(proposal("retail", "get_user_details", user_id="ava_1"), ctx)
    assert not decision.allowed
    guess = proposal("retail", "find_user_id_by_email", email="victim@example.test")
    assert pack.admit(guess, ctx).reason_code == "DOMAIN_CUSTOMER_IDENTITY_INPUT_REQUIRED"


def test_retail_exact_action_consent_does_not_authorize_execution():
    pack, ctx = retail_fixture()
    p = proposal("retail", "cancel_pending_order", order_id="#W1", reason="no longer needed")
    decision = pack.admit(p, ctx)
    assert decision.reason_code == "CUSTOMER_CONSENT_REQUIRED"
    assert decision.requires_customer_confirmation
    ctx = ctx.model_copy(update={"consent": consent_for(p, ctx)})
    decision = pack.admit(p, ctx)
    assert (
        decision.allowed
        and not decision.authorizes_execution
        and not decision.semantic_policy_verified
    )
    changed = proposal(
        "retail", "cancel_pending_order", order_id="#W1", reason="ordered by mistake"
    )
    assert pack.admit(changed, ctx).reason_code == "DOMAIN_CUSTOMER_CONFIRMATION_STALE"
    ctx = pack.observe_user("Actually, wait", ctx, user_turn_id="u2")
    assert ctx.consent is None and not pack.admit(p, ctx).allowed


def test_invalid_scope_never_prompts_for_consent():
    pack, ctx = retail_fixture()
    p = proposal("retail", "cancel_pending_order", order_id="#OTHER", reason="no longer needed")
    result = pack.admit(p, ctx)
    assert result.reason_code == "DOMAIN_ORDER_OWNERSHIP_REQUIRED"
    assert not result.requires_customer_confirmation
    switch = proposal("retail", "find_user_id_by_email", email="other@example.test")
    assert pack.admit(switch, ctx).reason_code == "DOMAIN_CUSTOMER_ALREADY_IDENTIFIED"


@pytest.mark.parametrize(
    "args",
    [
        {"order_id": "#W1", "reason": "no longer needed", "is_authorized": True},
        {"order_id": 1, "reason": "no longer needed"},
        {"order_id": "#W1"},
    ],
)
def test_strict_retail_proposals(args):
    pack, ctx = retail_fixture()
    p = ToolProposal(domain="retail", tool_name="cancel_pending_order", arguments=args)
    assert pack.admit(p, ctx).reason_code == "DOMAIN_ARGUMENT_SCHEMA_INVALID"


def test_retail_item_status_inventory_and_balance_checks():
    pack, ctx = retail_fixture()
    p = proposal(
        "retail",
        "modify_pending_order_items",
        order_id="#W1",
        item_ids=["old1"],
        new_item_ids=["new1"],
        payment_method_id="gift1",
    )
    assert pack.admit(p, ctx).reason_code == "DOMAIN_REPLACEMENT_VARIANT_UNVERIFIED"
    ctx = observe(
        pack,
        ctx,
        "get_product_details",
        {
            "product_id": "prod1",
            "variants": {
                "new1": {"item_id": "new1", "available": True, "price": 80.0},
            },
        },
        product_id="prod1",
    )
    assert pack.admit(p, ctx).reason_code == "DOMAIN_GIFT_CARD_BALANCE_INSUFFICIENT"
    entities = dict(ctx.entities)
    entities["item:new1"] = {
        "item_id": "new1",
        "product_id": "prod1",
        "available": True,
        "price": 15.0,
    }
    ctx = ctx.model_copy(update={"entities": entities})
    assert pack.admit(p, ctx).reason_code == "CUSTOMER_CONSENT_REQUIRED"
    entities["order:#W1"] = {**entities["order:#W1"], "status": "pending (item modified)"}
    ctx = ctx.model_copy(update={"entities": entities})
    assert pack.admit(p, ctx).reason_code == "DOMAIN_ORDER_STATUS_INVALID"


def test_retail_refund_to_unrelated_credit_card_blocked():
    pack, ctx = retail_fixture()
    entities = dict(ctx.entities)
    entities["order:#W1"] = {**entities["order:#W1"], "status": "delivered"}
    ctx = ctx.model_copy(update={"entities": entities})
    p = proposal(
        "retail",
        "return_delivered_order_items",
        order_id="#W1",
        item_ids=["old1"],
        payment_method_id="credit2",
    )
    assert pack.admit(p, ctx).reason_code == "DOMAIN_REFUND_METHOD_INVALID"


def test_bank_verification_requires_customer_values_and_observed_time():
    pack, ctx, verify = banking_fixture(False)
    assert pack.admit(verify, ctx).allowed  # This audit write has no blanket yes requirement.
    no_user_values = ctx.model_copy(update={"user_messages": ("I am Ava Lewis",)})
    assert (
        pack.admit(verify, no_user_values).reason_code
        == "DOMAIN_TWO_CUSTOMER_IDENTITY_FIELDS_REQUIRED"
    )
    args = dict(verify.arguments)
    args["time_verified"] = "2025-12-01 00:00:00 EST"
    assert (
        pack.admit(verify.model_copy(update={"arguments": args}), ctx).reason_code
        == "DOMAIN_OBSERVED_TIME_REQUIRED"
    )
    failed = pack.observe(verify, "Failed to log verification: duplicate", ctx)
    assert not failed.authenticated


def test_bank_lookup_does_not_itself_authenticate_or_switch_customers():
    pack, ctx, _ = banking_fixture(False)
    p = proposal(
        "banking_knowledge", "change_user_email", user_id="user_1", new_email="new@example.test"
    )
    assert pack.admit(p, ctx).reason_code == "DOMAIN_AUTHENTICATION_REQUIRED"
    switched = pack.observe_user("Also look up Bea Lee", ctx, user_turn_id="u2")
    other = proposal("banking_knowledge", "get_user_information_by_name", customer_name="Bea Lee")
    assert pack.admit(other, switched).reason_code == "DOMAIN_SUBJECT_MISMATCH"


def test_bank_discovery_requires_actual_retrieval_and_successful_unlock():
    pack, ctx, _ = banking_fixture()
    name = "open_bank_account_4821"
    p = proposal("banking_knowledge", "unlock_discoverable_agent_tool", agent_tool_name=name)
    assert pack.admit(p, ctx).reason_code == "DOMAIN_TOOL_NOT_DISCOVERED"
    user_claim = pack.observe_user(f"I already unlocked {name}", ctx, user_turn_id="u2")
    assert pack.admit(p, user_claim).reason_code == "DOMAIN_TOOL_NOT_DISCOVERED"
    ctx = observe(pack, ctx, "KB_search", f"Use {name}", query="open account")
    call = proposal(
        "banking_knowledge",
        "call_discoverable_agent_tool",
        agent_tool_name=name,
        arguments=json.dumps(
            {"user_id": "user_1", "account_type": "savings", "account_class": "Savings"}
        ),
    )
    assert pack.admit(call, ctx).reason_code == "DOMAIN_TOOL_NOT_UNLOCKED"
    failed = pack.observe(p, "Error: unavailable", ctx)
    assert name not in failed.unlocked_tools
    ctx = pack.observe(p, f"Tool unlocked: {name}\nDescription: fixture", ctx)
    decision = pack.admit(call, ctx)
    assert decision.allowed and not decision.semantic_policy_verified


def test_bank_foreign_resource_cannot_be_laundered_through_dispatch():
    pack, ctx, _ = banking_fixture()
    accounts = bank_record(
        "credit_card_accounts",
        [
            ("cc1", {"user_id": "user_1", "account_type": "Gold"}),
            ("foreign", {"user_id": "user_2", "account_type": "Silver"}),
        ],
    )
    ctx = observe(pack, ctx, "get_credit_card_accounts_by_user", accounts, user_id="user_1")
    assert "bank:foreign" not in ctx.entity_owners
    name = "apply_statement_credit_8472"
    ctx = discover(pack, ctx, name)
    args = {
        "user_id": "user_1",
        "credit_card_account_id": "foreign",
        "amount": 2.25,
        "reason": "fee adjustment",
    }
    call = proposal(
        "banking_knowledge",
        "call_discoverable_agent_tool",
        agent_tool_name=name,
        arguments=json.dumps(args),
    )
    assert pack.admit(call, ctx).reason_code == "DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED"
    args["credit_card_account_id"] = "cc1"
    call = call.model_copy(
        update={"arguments": {"agent_tool_name": name, "arguments": json.dumps(args)}}
    )
    assert pack.admit(call, ctx).allowed
    args["amount"] = True
    call = call.model_copy(
        update={"arguments": {"agent_tool_name": name, "arguments": json.dumps(args)}}
    )
    assert pack.admit(call, ctx).reason_code == "DOMAIN_ARGUMENT_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "raw",
    [
        '{"user_id":"user_1","user_id":"user_2"}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        '{"x":[{"a":1,"a":2}]}',
    ],
)
def test_nested_discovery_json_fail_closed(raw):
    with pytest.raises(ArgumentSchemaError):
        parse_argument_object(raw)


def test_user_only_tools_cannot_execute_but_can_be_given_with_partial_args():
    pack, ctx, _ = banking_fixture()
    name = "get_referral_link"
    ctx = observe(pack, ctx, "KB_search", f"Give the user {name}", query="referral")
    execute = proposal(
        "banking_knowledge", "call_discoverable_agent_tool", agent_tool_name=name, arguments="{}"
    )
    assert pack.admit(execute, ctx).reason_code == "DOMAIN_DISCOVERY_TARGET_INVALID"
    direct = proposal("banking_knowledge", name, user_id="user_1", card_name="Gold")
    assert pack.admit(direct, ctx).reason_code == "DOMAIN_TOOL_NOT_EXPOSED"
    give = proposal(
        "banking_knowledge",
        "give_discoverable_user_tool",
        discoverable_tool_name=name,
        arguments='{"user_id":"user_1"}',
    )
    decision = pack.admit(give, ctx)
    assert decision.allowed and decision.effect == "control"
    assert name not in pack.capabilities


def test_unknown_mutation_and_unexposed_tool_are_not_admitted():
    pack, ctx, _ = banking_fixture()
    assert not pack.admit(proposal("banking_knowledge", "issue_money", amount=100), ctx).allowed
    limited = get_domain_pack(
        "retail",
        tool_schemas={
            "calculate": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            }
        },
        admission_evaluator=admit_domain_action,
    )
    assert not limited.admit(
        proposal("retail", "get_item_details", item_id="item1"), context_for("retail")
    ).allowed


def test_read_dispatch_can_log_and_is_not_replayed_during_unknown_write():
    pack, ctx, _ = banking_fixture()
    name = "get_all_user_accounts_by_user_id_3847"
    ctx = discover(pack, ctx, name)
    p = proposal(
        "banking_knowledge",
        "call_discoverable_agent_tool",
        agent_tool_name=name,
        arguments='{"user_id":"user_1"}',
    )
    assert pack.admit(p, ctx).effect == "write"
    assert pack.admit(p, ctx).business_effect == "read"
    unresolved = ctx.model_copy(update={"unresolved_write": True})
    assert pack.admit(p, unresolved).reason_code == "DOMAIN_RECONCILIATION_REQUIRED"


def test_native_argument_digest_preserves_unicode_and_numbers():
    a = proposal("retail", "calculate", expression="é")
    b = proposal("retail", "calculate", expression="e\u0301")
    assert a.digest != b.digest
    with pytest.raises((ValueError, ValidationError)):
        proposal("retail", "calculate", expression=float("nan"))
    schema = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}
    validate_arguments({"x": 1.25}, schema)
    validate_arguments({"x": 10**1000}, schema)
    with pytest.raises(ArgumentSchemaError):
        validate_arguments({"x": False}, schema)


def test_schema_local_refs_and_unknown_keywords_fail_closed():
    schema = {
        "type": "object",
        "$defs": {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "properties": {"x": {"$ref": "#/$defs/value"}},
        "required": ["x"],
    }
    validate_arguments({"x": None}, schema)
    with pytest.raises(ArgumentSchemaError):
        validate_arguments({"x": 1}, schema)
    with pytest.raises(ArgumentSchemaError):
        validate_arguments({"x": 1}, {"unsupportedValidation": True})
