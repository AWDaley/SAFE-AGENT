"""Actual pinned tool implementations with an entirely authored in-memory DB.

No benchmark tasks, official database, hidden evaluator, hosted model, or
retrieval service is used. This checks result formatting and nested execution.
"""

import json

import pytest

from safe_agent.domains import DomainContext, ToolProposal, get_domain_pack


def test_actual_bank_results_authenticate_and_scope_inner_dispatch(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip(
        "tau2", reason="Pinned tau integration dependency is optional outside benchmark CI"
    )
    evaluator = pytest.importorskip("tal.domain_admission").admit_domain_action
    from tau2.domains.banking_knowledge.data_model import DatabaseTable, TransactionalDB
    from tau2.domains.banking_knowledge.tools import KnowledgeTools

    customer = {
        "user_id": "customer_fixture",
        "name": "Ava Example",
        "email": "ava@example.test",
        "address": "1 Example Way",
        "phone_number": "555-0199",
        "date_of_birth": "02/03/1980",
    }
    db = TransactionalDB(
        users=DatabaseTable(data={"customer_fixture": customer}),
        credit_card_accounts=DatabaseTable(
            data={
                "row_fixture": {
                    "account_id": "card_fixture",
                    "user_id": "customer_fixture",
                    "class": "Standard",
                },
                "row_other": {
                    "account_id": "card_other",
                    "user_id": "other_customer",
                    "class": "Standard",
                },
            }
        ),
    )
    tools = KnowledgeTools(db)
    pack = get_domain_pack("banking_knowledge", admission_evaluator=evaluator)
    context = pack.observe_user(
        "I am Ava Example. My email is ava@example.test and phone number is 555-0199.",
        DomainContext(
            episode_id="ep_fixture",
            domain="banking_knowledge",
            policy_hash="sha256:" + "1" * 64,
            observation_hash="sha256:" + "2" * 64,
            state_version=0,
        ),
        user_turn_id="user1",
    )

    def call(tool_name, **args):
        nonlocal context
        p = ToolProposal(domain="banking_knowledge", tool_name=tool_name, arguments=args)
        decision = pack.admit(p, context)
        assert decision.allowed, decision.reason_code
        result = getattr(tools, tool_name)(**args)
        assert not result.startswith(("Error", "Failed")), result
        context = pack.observe(p, result, context)
        return result

    call("get_user_information_by_name", customer_name="Ava Example")
    assert context.subject_id == "customer_fixture" and not context.authenticated
    call("get_current_time")
    call("log_verification", **customer, time_verified="2025-11-14 03:40:00 EST")
    assert context.authenticated and len(db.verification_history.data) == 1
    call("get_credit_card_accounts_by_user", user_id="customer_fixture")
    assert context.entity_owners["bank:card_fixture"] == "customer_fixture"
    assert "bank:card_other" not in context.entity_owners
    inner = "apply_statement_credit_8472"
    retrieval = ToolProposal(
        domain="banking_knowledge", tool_name="KB_search", arguments={"query": "fixture"}
    )
    # An authored retrieval observation, not a hidden-policy grant.
    context = pack.observe(retrieval, f"Authored policy fixture permits {inner}.", context)
    call("unlock_discoverable_agent_tool", agent_tool_name=inner)
    args = {
        "user_id": "customer_fixture",
        "credit_card_account_id": "card_other",
        "amount": 2.25,
        "reason": "error_correction",
    }
    foreign = ToolProposal(
        domain="banking_knowledge",
        tool_name="call_discoverable_agent_tool",
        arguments={"agent_tool_name": inner, "arguments": json.dumps(args)},
    )
    assert pack.admit(foreign, context).reason_code == "DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED"
    assert not db.credit_card_transaction_history.data
    args["credit_card_account_id"] = "card_fixture"
    result = call("call_discoverable_agent_tool", agent_tool_name=inner, arguments=json.dumps(args))
    assert result.startswith("Statement credit applied successfully.")
    assert len(db.credit_card_transaction_history.data) == 1
    assert (
        next(iter(db.credit_card_transaction_history.data.values()))["transaction_amount"]
        == "-$2.25"
    )
    assert len(db.agent_discoverable_tools.data) == 1
