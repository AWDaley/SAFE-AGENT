"""Pinned native argument names must preserve scoped opaque references.

All customer and resource records here are authored. Native tool observations
exercise the real banking presentation format without any official tasks.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from safe_agent.conversation.intake import ProjectionDenied, ReferenceVault
from safe_agent.domains import DomainContext, ToolProposal, get_domain_pack

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SUBJECT = "authored_customer"


@pytest.fixture
def banking_records(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    from tal.domain_admission import admit_domain_action
    from tau2.domains.banking_knowledge.data_model import DatabaseTable, TransactionalDB
    from tau2.domains.banking_knowledge.tools import KnowledgeTools

    customer = {
        "user_id": SUBJECT,
        "name": "Ava Example",
        "email": "ava@example.test",
        "address": "1 Example Way",
        "phone_number": "415-555-0199",
        "date_of_birth": "02/03/1980",
    }
    foreign = {**customer, "user_id": "foreign_customer", "name": "Other Example"}
    db = TransactionalDB(
        users=DatabaseTable(data={SUBJECT: customer, "foreign_customer": foreign}),
        credit_card_accounts=DatabaseTable(data={
            "row_owned": {"account_id": "card_owned", "user_id": SUBJECT, "class": "Standard"},
            "row_foreign": {
                "account_id": "card_foreign", "user_id": "foreign_customer", "class": "Standard",
            },
        }),
        credit_card_transaction_history=DatabaseTable(data={
            "row_transaction": {
                "transaction_id": "business_transaction", "user_id": SUBJECT,
                "rewards_earned": "0 points",
            },
            "row_foreign_transaction": {
                "transaction_id": "foreign_transaction", "user_id": "foreign_customer",
                "rewards_earned": "0 points",
            },
        }),
    )
    tools = KnowledgeTools(db)
    pack = get_domain_pack("banking_knowledge", admission_evaluator=admit_domain_action)
    context = pack.observe_user(
        "I am Ava Example. My email is ava@example.test and phone is 415-555-0199.",
        DomainContext(
            episode_id="ep_native_arguments", domain="banking_knowledge",
            policy_hash="sha256:" + "1" * 64, observation_hash="sha256:" + "2" * 64,
            state_version=0,
        ),
        user_turn_id="authored_user_turn",
    )
    for name, args in (
        ("get_user_information_by_name", {"customer_name": customer["name"]}),
        ("get_current_time", {}),
        ("log_verification", {**customer, "time_verified": "2025-11-14 03:40:00 EST"}),
        ("get_credit_card_accounts_by_user", {"user_id": SUBJECT}),
        ("get_credit_card_transactions_by_user", {"user_id": SUBJECT}),
    ):
        proposal = ToolProposal(domain="banking_knowledge", tool_name=name, arguments=args)
        decision = pack.admit(proposal, context)
        assert decision.allowed, decision.reason_code
        result = getattr(tools, name)(**args)
        context = pack.observe(proposal, result, context)
    assert context.authenticated
    vault = ReferenceVault(
        episode_id=context.episode_id, actor_id="authored_actor",
        deadline=NOW + timedelta(minutes=10),
        tools=[{"name": name} for name in (
            "call_discoverable_agent_tool", "give_discoverable_user_tool", "change_user_email",
        )],
        resource_subjects=context.entity_owners,
    )
    # Both owned and foreign records use the real upstream text formatter.
    for name, args in (
        ("get_user_information_by_id", {"user_id": SUBJECT}),
        ("get_credit_card_accounts_by_user", {"user_id": SUBJECT}),
        ("get_credit_card_accounts_by_user", {"user_id": "foreign_customer"}),
        ("get_credit_card_transactions_by_user", {"user_id": SUBJECT}),
        ("get_credit_card_transactions_by_user", {"user_id": "foreign_customer"}),
    ):
        vault.project(getattr(tools, name)(**args), source="tool", subject_id=None)
    return vault, tools, pack, context


def reference(vault, value, kind):
    return next(record["reference"] for record in vault.records
                if record["value"] == value and record["kind"] == kind)


def test_user_grant_resolves_only_scoped_nested_arguments(banking_records):
    vault, tools, _, _ = banking_records
    token = reference(vault, SUBJECT, "user_id")
    args = {"discoverable_tool_name": "get_referral_link",
            "arguments": json.dumps({"user_id": token, "card_name": "Platinum Rewards Card"})}
    resolved = vault.resolve_arguments(
        "give_discoverable_user_tool", args, now=NOW, subject_id=SUBJECT,
    )
    assert json.loads(resolved["arguments"]) == {
        "user_id": SUBJECT, "card_name": "Platinum Rewards Card",
    }
    assert not tools.db.user_discoverable_tools.data
    assert not tools.db.referrals.data
    with pytest.raises(ProjectionDenied, match="SCOPE_DENIED"):
        vault.resolve_arguments("give_discoverable_user_tool", args,
                                now=NOW, subject_id="foreign_customer")
    with pytest.raises(ProjectionDenied, match="SCOPE_DENIED"):
        vault.resolve_arguments("give_discoverable_user_tool", args,
                                now=NOW + timedelta(hours=1), subject_id=SUBJECT)
    restricted = vault.reference(SUBJECT, kind="user_id", source="tool", subject_id=SUBJECT,
                                 operations={"call_discoverable_agent_tool"})
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve_arguments(
            "give_discoverable_user_tool",
            {**args, "arguments": json.dumps({"user_id": restricted})},
            now=NOW, subject_id=SUBJECT,
        )
    assert vault.resolve_arguments(
        "give_discoverable_user_tool", {"discoverable_tool_name": "get_referral_link"},
        now=NOW, subject_id=SUBJECT,
    ) == {"discoverable_tool_name": "get_referral_link"}


@pytest.mark.parametrize("raw", [
    '[]', '{"user_id":"a","user_id":"b"}', '{"amount":NaN}', 'not json', {"user_id": "x"},
])
def test_invalid_user_grant_json_is_a_repairable_projection_denial(banking_records, raw):
    vault, _, _, _ = banking_records
    with pytest.raises(ProjectionDenied, match="INVALID_NESTED_ARGUMENTS"):
        vault.resolve_arguments(
            "give_discoverable_user_tool",
            {"discoverable_tool_name": "get_referral_link", "arguments": raw},
            now=NOW, subject_id=SUBJECT,
        )


def test_observed_account_aliases_allow_native_dispatch_but_not_wrong_kinds_or_owners(
    banking_records,
):
    vault, tools, pack, context = banking_records
    account = reference(vault, "card_owned", "account_id")
    for field in ("account_id", "credit_card_account_id", "checking_account_id",
                  "source_account_id", "destination_account_id"):
        assert vault.resolve({field: account}, operation="call_discoverable_agent_tool",
                             now=NOW, subject_id=SUBJECT) == {field: "card_owned"}
    for field in ("card_id", "transaction_id", "user_id", "made_up_account_id"):
        with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
            vault.resolve({field: account}, operation="call_discoverable_agent_tool",
                          now=NOW, subject_id=SUBJECT)
    with pytest.raises(ProjectionDenied, match="SCOPE_DENIED"):
        vault.resolve({"credit_card_account_id": reference(vault, "card_foreign", "account_id")},
                      operation="call_discoverable_agent_tool", now=NOW, subject_id=SUBJECT)
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve({"credit_card_account_id": reference(vault, SUBJECT, "user_id")},
                      operation="call_discoverable_agent_tool", now=NOW, subject_id=SUBJECT)

    inner = "apply_statement_credit_8472"
    search = ToolProposal(domain="banking_knowledge", tool_name="KB_search",
                          arguments={"query": "authored fixture"})
    context = pack.observe(search, "Authored document describes " + inner, context)
    unlock = ToolProposal(domain="banking_knowledge", tool_name="unlock_discoverable_agent_tool",
                          arguments={"agent_tool_name": inner})
    context = pack.observe(unlock, tools.unlock_discoverable_agent_tool(inner), context)
    args = vault.resolve_arguments(
        "call_discoverable_agent_tool",
        {"agent_tool_name": inner, "arguments": json.dumps({
            "user_id": reference(vault, SUBJECT, "user_id"),
            "credit_card_account_id": account, "amount": 2.25, "reason": "error_correction",
        })},
        now=NOW, subject_id=SUBJECT,
    )
    proposal = ToolProposal(domain="banking_knowledge", tool_name="call_discoverable_agent_tool",
                            arguments=args)
    assert pack.admit(proposal, context).allowed
    before = len(tools.db.credit_card_transaction_history.data)
    result = tools.call_discoverable_agent_tool(**args)
    assert result.startswith("Statement credit applied successfully.")
    assert len(tools.db.credit_card_transaction_history.data) == before + 1
    assert len(tools.db.agent_discoverable_tools.data) == 1


def test_native_row_keys_have_only_the_pinned_table_kind_and_observed_owner(banking_records):
    vault, tools, pack, context = banking_records
    account_row = reference(vault, "row_owned", "account_id")
    transaction_row = reference(vault, "row_transaction", "transaction_id")
    assert vault.resolve({"credit_card_account_id": account_row},
                         operation="call_discoverable_agent_tool", now=NOW,
                         subject_id=SUBJECT) == {"credit_card_account_id": "row_owned"}
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve({"transaction_id": account_row}, operation="call_discoverable_agent_tool",
                      now=NOW, subject_id=SUBJECT)
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve({"account_id": transaction_row}, operation="call_discoverable_agent_tool",
                      now=NOW, subject_id=SUBJECT)
    with pytest.raises(ProjectionDenied, match="SCOPE_DENIED"):
        vault.resolve(
            {"transaction_id": reference(vault, "row_foreign_transaction", "transaction_id")},
            operation="call_discoverable_agent_tool", now=NOW, subject_id=SUBJECT,
        )
    inner = "update_transaction_rewards_3847"
    search = ToolProposal(domain="banking_knowledge", tool_name="KB_search",
                          arguments={"query": "authored fixture"})
    context = pack.observe(search, "Authored document describes " + inner, context)
    unlock = ToolProposal(domain="banking_knowledge", tool_name="unlock_discoverable_agent_tool",
                          arguments={"agent_tool_name": inner})
    context = pack.observe(unlock, tools.unlock_discoverable_agent_tool(inner), context)
    args = vault.resolve_arguments(
        "call_discoverable_agent_tool",
        {"agent_tool_name": inner, "arguments": json.dumps({
            "transaction_id": transaction_row, "new_rewards_earned": "125 points",
        })}, now=NOW, subject_id=SUBJECT,
    )
    proposal = ToolProposal(domain="banking_knowledge", tool_name="call_discoverable_agent_tool",
                            arguments=args)
    assert pack.admit(proposal, context).allowed
    assert tools.call_discoverable_agent_tool(**args).startswith(
        "Transaction rewards updated successfully.",
    )
    assert tools.db.credit_card_transaction_history.data["row_transaction"][
        "rewards_earned"
    ] == "125 points"
    assert tools.db.credit_card_transaction_history.data["row_foreign_transaction"][
        "rewards_earned"
    ] == "0 points"


def test_native_email_and_shipping_fields_keep_identity_kinds(banking_records):
    vault, tools, pack, context = banking_records
    new_email = "new.ava@example.test"
    incoming = vault.project_text(new_email, source="customer", subject_id=SUBJECT)
    args = vault.resolve_arguments(
        "change_user_email", {"user_id": reference(vault, SUBJECT, "user_id"),
                              "new_email": incoming},
        now=NOW, subject_id=SUBJECT,
    )
    proposal = ToolProposal(domain="banking_knowledge", tool_name="change_user_email",
                            arguments=args)
    assert pack.admit(proposal, context).allowed
    assert tools.change_user_email(**args).startswith("Email updated successfully.")
    assert tools.db.users.data[SUBJECT]["email"] == new_email
    address = reference(vault, "1 Example Way", "address")
    assert vault.resolve({"shipping_address": address}, operation="call_discoverable_agent_tool",
                         now=NOW, subject_id=SUBJECT) == {"shipping_address": "1 Example Way"}
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve({"shipping_address": incoming}, operation="call_discoverable_agent_tool",
                      now=NOW, subject_id=SUBJECT)


def test_retail_new_item_ids_accept_observed_variants_without_promoting_product_ids(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    from tau2.domains.retail.data_model import Product, RetailDB, Variant
    from tau2.domains.retail.tools import RetailTools

    product = Product(name="Authored mug", product_id="product_authored", variants={
        "item_blue": Variant(item_id="item_blue", options={"color": "blue"},
                             available=True, price=8.0),
    })
    tools = RetailTools(RetailDB(products={product.product_id: product}, users={}, orders={}))
    observed = tools.get_product_details(product.product_id).model_dump(mode="json")
    vault = ReferenceVault(episode_id="ep_authored_retail", actor_id="authored_actor",
                            deadline=NOW + timedelta(minutes=10),
                            tools=[{"name": "modify_pending_order_items"}])
    projected = vault.project(observed, source="tool", subject_id=None)
    item = next(iter(projected["variants"].values()))["item_id"]
    assert vault.resolve_arguments(
        "modify_pending_order_items", {"new_item_ids": [item, item]},
        now=NOW, subject_id=None,
    ) == {"new_item_ids": ["item_blue", "item_blue"]}
    with pytest.raises(ProjectionDenied, match="KIND_OR_OPERATION_DENIED"):
        vault.resolve_arguments(
            "modify_pending_order_items", {"new_item_ids": [projected["product_id"]]},
            now=NOW, subject_id=None,
        )
