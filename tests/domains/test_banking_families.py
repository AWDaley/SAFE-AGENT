"""Authored banking family probes against the pinned public native tools.

The database and retrieval observations are synthetic and authored here. No
official tasks, database, evaluator, retrieval service, or model calls are used.
These cases check resource scope and concrete native effects, not financial
eligibility rules, privacy projection, consent, or end-to-end benchmark quality.
"""

import json
from dataclasses import dataclass
from typing import Any

import pytest

from safe_agent.domains import DomainContext, DomainPack, ToolProposal, get_domain_pack


def _proposal(op: str, args: dict[str, Any]) -> ToolProposal:
    return ToolProposal(domain="banking_knowledge", tool_name=op, arguments=args)


@dataclass
class _Bank:
    db: Any
    tools: Any
    pack: DomainPack
    context: DomainContext

    def call(self, op: str, *, expected_error: str | None = None, **args: Any) -> str:
        proposal = _proposal(op, args)
        decision = self.pack.admit(proposal, self.context)
        assert decision.allowed, (op, decision.reason_code)
        assert decision.tal_decision_hash
        result = getattr(self.tools, op)(**args)
        if expected_error is None:
            assert not result.lstrip().startswith(("Error", "Failed")), (op, result)
        else:
            assert result.startswith(expected_error), (op, result)
        self.context = self.pack.observe(proposal, result, self.context)
        if expected_error is not None:
            assert not self.context.observations[-1].success
        return result

    def discover(self, op: str) -> None:
        if op in self.context.unlocked_tools:
            return
        # Names come from an authored observation, never hidden task/policy data.
        self.context = self.pack.observe(
            _proposal("KB_search", {"query": "authored operation-family fixture"}),
            f"Authored fixture lists the tool {op}.",
            self.context,
        )
        self.call("unlock_discoverable_agent_tool", agent_tool_name=op)

    def inner(self, op: str, *, expected_error: str | None = None, **args: Any) -> str:
        self.discover(op)
        return self.call(
            "call_discoverable_agent_tool",
            agent_tool_name=op,
            arguments=json.dumps(args),
            expected_error=expected_error,
        )

    def deny(
        self,
        op: str,
        *,
        expected_reason: str = "DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED",
        **args: Any,
    ) -> None:
        before = self.db.model_dump()
        proposal = _proposal(op, args)
        decision = self.pack.admit(proposal, self.context)
        assert not decision.allowed
        assert decision.reason_code == expected_reason
        # A denied proposal never reaches the native dispatcher.
        assert self.db.model_dump() == before

    def deny_inner(
        self,
        op: str,
        *,
        expected_reason: str = "DOMAIN_BANK_RESOURCE_OWNERSHIP_REQUIRED",
        **args: Any,
    ) -> None:
        self.discover(op)
        self.deny(
            "call_discoverable_agent_tool",
            expected_reason=expected_reason,
            agent_tool_name=op,
            arguments=json.dumps(args),
        )


@pytest.fixture
def bank(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip(
        "tau2", reason="Pinned tau integration dependency is optional outside benchmark CI"
    )
    evaluator = pytest.importorskip("tal.domain_admission").admit_domain_action
    import litellm
    from tau2.domains.banking_knowledge.data_model import DatabaseTable, TransactionalDB
    from tau2.domains.banking_knowledge.tools import KnowledgeTools

    def forbid_model(*args, **kwargs):
        pytest.fail("Authored native-tool probes must not call a model or embedding service")

    for method in ("completion", "acompletion", "embedding", "aembedding"):
        monkeypatch.setattr(litellm, method, forbid_model)

    customer = {
        "user_id": "customer_probe",
        "name": "Ava Example",
        "email": "ava@example.test",
        "address": "1 Example Way",
        "phone_number": "555-0199",
        "date_of_birth": "02/03/1980",
    }
    db = TransactionalDB(
        users=DatabaseTable(data={"customer_probe": customer}),
        accounts=DatabaseTable(
            data={
                "checking_probe": {
                    "account_id": "checking_probe",
                    "user_id": "customer_probe",
                    "class": "checking",
                    "status": "ACTIVE",
                    "current_holdings": "$1000.00",
                },
                "savings_probe": {
                    "account_id": "savings_probe",
                    "user_id": "customer_probe",
                    "class": "savings",
                    "status": "ACTIVE",
                    "current_holdings": "$50.00",
                },
                "foreign_account": {
                    "account_id": "foreign_account",
                    "user_id": "foreign_customer",
                    "class": "checking",
                    "status": "ACTIVE",
                    "current_holdings": "$0.00",
                },
            }
        ),
        credit_card_accounts=DatabaseTable(
            data={
                "credit_probe": {
                    "account_id": "credit_probe",
                    "user_id": "customer_probe",
                    "class": "Standard",
                    "status": "ACTIVE",
                    "current_balance": "$100.00",
                    "credit_limit": "$1000.00",
                },
                "foreign_credit": {
                    "account_id": "foreign_credit",
                    "user_id": "foreign_customer",
                    "current_balance": "$100.00",
                },
            }
        ),
        credit_card_transaction_history=DatabaseTable(
            data={
                # Native reward updates consume the row key, which need not equal
                # the business transaction_id returned in the same observation.
                "transaction_probe": {
                    "transaction_id": "business_transaction_probe",
                    "user_id": "customer_probe",
                    "credit_card_account_id": "credit_probe",
                    "rewards_earned": "10 points",
                    "transaction_amount": "$10.00",
                    "transaction_date": "11/01/2025",
                },
                "foreign_transaction": {
                    "transaction_id": "foreign_business_transaction",
                    "user_id": "foreign_customer",
                    "rewards_earned": "1 points",
                },
            }
        ),
        debit_cards=DatabaseTable(
            data={
                "debit_probe": {
                    "card_id": "debit_probe",
                    "account_id": "checking_probe",
                    "status": "ACTIVE",
                    "last_4_digits": "1234",
                    "pin_locked": True,
                    "pin_attempts_remaining": 0,
                },
                "foreign_card": {
                    "card_id": "foreign_card",
                    "account_id": "foreign_account",
                    "status": "ACTIVE",
                },
            }
        ),
    )
    pack = get_domain_pack("banking_knowledge", admission_evaluator=evaluator)
    context = pack.observe_user(
        "I am Ava Example. My email is ava@example.test and phone number is 555-0199.",
        DomainContext(
            episode_id="episode_family_probe",
            domain="banking_knowledge",
            policy_hash="sha256:" + "1" * 64,
            observation_hash="sha256:" + "2" * 64,
            state_version=0,
        ),
        user_turn_id="turn_probe",
    )
    harness = _Bank(db, KnowledgeTools(db), pack, context)
    harness.call("get_user_information_by_name", customer_name=customer["name"])
    harness.call("get_current_time")
    harness.call(
        "log_verification", **customer, time_verified=harness.context.entities["bank:current_time"]
    )
    assert harness.context.authenticated
    harness.call("get_credit_card_accounts_by_user", user_id="customer_probe")
    harness.call("get_credit_card_transactions_by_user", user_id="customer_probe")
    harness.inner("get_all_user_accounts_by_user_id_3847", user_id="customer_probe")
    harness.inner("get_debit_cards_by_account_id_7823", account_id="checking_probe")
    for resource in ("checking_probe", "savings_probe", "credit_probe", "debit_probe"):
        assert harness.context.entity_owners["bank:" + resource] == "customer_probe"
    for resource in ("foreign_account", "foreign_credit", "foreign_card", "foreign_transaction"):
        assert "bank:" + resource not in harness.context.entity_owners
    return harness


def test_transfer_scopes_both_accounts_and_moves_funds(bank):
    op = "transfer_funds_between_bank_accounts_7291"
    bank.deny_inner(
        op, source_account_id="checking_probe", destination_account_id="foreign_account", amount=10.0
    )
    bank.deny_inner(
        op, source_account_id="foreign_account", destination_account_id="savings_probe", amount=10.0
    )
    bank.inner(
        op, source_account_id="checking_probe", destination_account_id="savings_probe", amount=10.0
    )
    assert bank.db.accounts.data["checking_probe"]["current_holdings"] == "$990.00"
    assert bank.db.accounts.data["savings_probe"]["current_holdings"] == "$60.00"


def test_statement_credit_scopes_account_and_adds_credit_record(bank):
    op = "apply_statement_credit_8472"
    args = {"user_id": "customer_probe", "amount": 2.25, "reason": "error_correction"}
    bank.deny_inner(op, **args, credit_card_account_id="foreign_credit")
    before = set(bank.db.credit_card_transaction_history.data)
    bank.inner(op, **args, credit_card_account_id="credit_probe")
    created = set(bank.db.credit_card_transaction_history.data) - before
    assert len(created) == 1
    record = bank.db.credit_card_transaction_history.data[created.pop()]
    assert record["transaction_amount"] == "-$2.25"
    assert record["user_id"] == "customer_probe"
    assert record["credit_card_account_id"] == "credit_probe"


def test_card_freeze_uses_observed_card_ownership(bank):
    op = "freeze_debit_card_3892"
    bank.deny_inner(op, card_id="foreign_card")
    bank.inner(op, card_id="debit_probe")
    assert bank.db.debit_cards.data["debit_probe"]["status"] == "FROZEN"
    assert bank.db.debit_cards.data["foreign_card"]["status"] == "ACTIVE"


def test_dispute_scopes_transaction_and_creates_customer_case(bank):
    op = "file_credit_card_transaction_dispute_4829"
    args = {
        "card_action": "keep_active",
        "card_last_4_digits": "1234",
        "full_name": "Ava Example",
        "user_id": "customer_probe",
        "phone": "555-0199",
        "email": "ava@example.test",
        "address": "1 Example Way",
        "contacted_merchant": True,
        "purchase_date": "11/01/2025",
        "issue_noticed_date": "11/02/2025",
        "dispute_reason": "duplicate_charge",
        "resolution_requested": "full_refund",
        "eligible_for_provisional_credit": False,
    }
    bank.deny_inner(op, **args, transaction_id="foreign_transaction")
    bank.inner(op, **args, transaction_id="business_transaction_probe")
    assert len(bank.db.transaction_disputes.data) == 1
    record = next(iter(bank.db.transaction_disputes.data.values()))
    assert record["transaction_id"] == "business_transaction_probe"
    assert record["user_id"] == "customer_probe"
    assert record["status"] == "SUBMITTED"


def test_rewards_update_scopes_native_row_key_and_changes_points(bank):
    op = "update_transaction_rewards_3847"
    bank.deny_inner(op, transaction_id="foreign_transaction", new_rewards_earned="150 points")
    assert bank.context.entity_owners["bank:transaction_probe"] == "customer_probe"
    assert bank.context.entity_owners["bank:business_transaction_probe"] == "customer_probe"
    bank.inner(op, transaction_id="transaction_probe", new_rewards_earned="150 points")
    assert bank.db.credit_card_transaction_history.data["transaction_probe"]["rewards_earned"] == (
        "150 points"
    )
    assert bank.db.credit_card_transaction_history.data["foreign_transaction"]["rewards_earned"] == (
        "1 points"
    )


def test_credit_limit_approval_scopes_account_and_records_change(bank):
    op = "approve_credit_limit_increase_5847"
    args = {"user_id": "customer_probe", "new_credit_limit": 1500}
    bank.deny_inner(op, **args, credit_card_account_id="foreign_credit")
    bank.inner(op, **args, credit_card_account_id="credit_probe")
    assert bank.db.credit_card_accounts.data["credit_probe"]["credit_limit"] == "$1500.00"
    assert len(bank.db.credit_limit_increase_requests.data) == 1
    record = next(iter(bank.db.credit_limit_increase_requests.data.values()))
    assert record["credit_card_account_id"] == "credit_probe"
    assert record["status"] == "APPROVED"


def test_account_lifecycle_requires_observation_before_closure(bank):
    open_op = "open_bank_account_4821"
    args = {"account_type": "checking", "account_class": "Light Blue Account"}
    bank.deny_inner(
        open_op, **args, user_id="foreign_customer", expected_reason="DOMAIN_SUBJECT_MISMATCH"
    )
    before = set(bank.db.accounts.data)
    bank.inner(open_op, **args, user_id="customer_probe")
    created = set(bank.db.accounts.data) - before
    assert len(created) == 1
    account_id = created.pop()
    assert bank.db.accounts.data[account_id]["status"] == "OPEN"
    close_op = "close_bank_account_7392"
    bank.deny_inner(close_op, account_id="foreign_account")
    # A successful write's prose alone does not establish resource ownership.
    bank.deny_inner(close_op, account_id=account_id)
    bank.inner("get_all_user_accounts_by_user_id_3847", user_id="customer_probe")
    bank.inner(close_op, account_id=account_id, reason="Authored customer closure")
    assert bank.db.accounts.data[account_id]["status"] == "CLOSED"
    assert bank.db.accounts.data[account_id]["closure_reason"] == "Authored customer closure"


def test_card_payment_scopes_both_accounts_and_changes_balances(bank):
    op = "pay_credit_card_from_checking_9182"
    args = {"user_id": "customer_probe", "amount": 25.0}
    bank.deny_inner(
        op, **args, checking_account_id="foreign_account", credit_card_account_id="credit_probe"
    )
    bank.deny_inner(
        op, **args, checking_account_id="checking_probe", credit_card_account_id="foreign_credit"
    )
    bank.inner(op, **args, checking_account_id="checking_probe", credit_card_account_id="credit_probe")
    assert bank.db.accounts.data["checking_probe"]["current_holdings"] == "975.00"
    assert bank.db.credit_card_accounts.data["credit_probe"]["current_balance"] == "$75.00"


def test_credit_card_replacement_scopes_order_and_supporting_read(bank):
    op = "order_replacement_credit_card_7291"
    args = {
        "user_id": "customer_probe",
        "shipping_address": "1 Example Way",
        "reason": "lost",
    }
    bank.deny_inner(op, **args, credit_card_account_id="foreign_credit")
    bank.inner(op, **args, credit_card_account_id="credit_probe")
    assert len(bank.db.credit_card_orders.data) == 1
    order_id, order = next(iter(bank.db.credit_card_orders.data.items()))
    assert order["credit_card_account_id"] == "credit_probe"
    assert order["user_id"] == "customer_probe"
    assert order["shipping_address"] == "1 Example Way"
    assert order["status"] == "ORDERED" and order["old_card_cancelled"] is True
    assert bank.db.credit_card_accounts.data["credit_probe"]["status"] == "CLOSED"

    read_op = "get_pending_replacement_orders_5765"
    bank.deny_inner(read_op, credit_card_account_id="foreign_credit")
    result = bank.inner(read_op, credit_card_account_id="credit_probe")
    assert order_id in result
    assert bank.context.entity_owners["bank:" + order_id] == "customer_probe"


def test_credit_card_closure_scopes_account_after_balance_payment(bank):
    op = "close_credit_card_account_7834"
    bank.deny_inner(op, user_id="customer_probe", credit_card_account_id="foreign_credit")
    bank.inner(
        "pay_credit_card_from_checking_9182",
        user_id="customer_probe",
        checking_account_id="checking_probe",
        credit_card_account_id="credit_probe",
        amount=100.0,
    )
    bank.inner(op, user_id="customer_probe", credit_card_account_id="credit_probe")
    account = bank.db.credit_card_accounts.data["credit_probe"]
    assert account["current_balance"] == "$0.00"
    assert account["status"] == "CLOSED" and account["closed_by"] == "customer_probe"
    assert account["closed_date"]


def test_fee_correction_scopes_account_and_posts_readable_refund(bank):
    op = "apply_checking_account_credit_5829"
    args = {"amount": 7.50, "credit_type": "fee_refund"}
    bank.deny_inner(op, **args, account_id="foreign_account")
    bank.inner(op, **args, account_id="checking_probe")
    assert bank.db.accounts.data["checking_probe"]["current_holdings"] == "$1007.50"
    assert len(bank.db.bank_account_transaction_history.data) == 1
    transaction_id, record = next(iter(bank.db.bank_account_transaction_history.data.items()))
    assert record["account_id"] == "checking_probe"
    assert record["type"] == "fee_refund" and record["status"] == "posted"
    assert record["amount"] == 7.50

    read_op = "get_bank_account_transactions_9173"
    bank.deny_inner(read_op, account_id="foreign_account")
    result = bank.inner(read_op, account_id="checking_probe")
    assert transaction_id in result
    assert bank.context.entity_owners["bank:" + transaction_id] == "customer_probe"


def test_debit_card_closure_scopes_card_and_records_permanent_cancellation(bank):
    op = "close_debit_card_4721"
    bank.deny_inner(op, card_id="foreign_card", reason="stolen")
    bank.inner(op, card_id="debit_probe", reason="stolen")
    card = bank.db.debit_cards.data["debit_probe"]
    assert card["status"] == "CLOSED" and card["closure_reason"] == "stolen"
    assert card["closed_date"]
    assert bank.db.debit_cards.data["foreign_card"]["status"] == "ACTIVE"


def test_pin_reset_requires_owned_card_and_native_card_verification(bank):
    op = "reset_debit_card_pin_6284"
    bank.deny_inner(op, card_id="foreign_card", last_4_digits="1234", new_pin="4826")
    before = bank.db.debit_cards.model_dump()
    bank.inner(
        op,
        card_id="debit_probe",
        last_4_digits="9999",
        new_pin="4826",
        expected_error="Error: Card verification failed.",
    )
    assert bank.db.debit_cards.model_dump() == before
    bank.inner(op, card_id="debit_probe", last_4_digits="1234", new_pin="4826")
    card = bank.db.debit_cards.data["debit_probe"]
    assert card["pin_locked"] is False and card["pin_attempts_remaining"] == 3
    assert card["pin_last_changed"]


def test_handoff_accepts_native_reason_and_rejects_unknown_route(bank):
    op = "transfer_to_human_agents"
    summary = "Authored customer requests security-team help with an unfamiliar charge."
    bank.deny(
        op,
        summary=summary,
        reason="invented_transfer_route",
        expected_reason="DOMAIN_ARGUMENT_SCHEMA_INVALID",
    )
    result = bank.call(op, summary=summary, reason="fraud_or_security_concern")
    assert result.startswith("Transfer successful (reason: fraud_or_security_concern).")
    assert bank.context.observations[-1].tool_name == op
    assert bank.context.observations[-1].success
