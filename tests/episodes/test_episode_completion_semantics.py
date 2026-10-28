"""Completion questions stay usable without admitting unsupported success claims."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from safe_agent.benchmarks.tau3.protocol import Message, Observation
from safe_agent.contracts.episode_inference import MessageCandidate
from safe_agent.conversation.intake import ProjectionDenied, ReferenceVault
from safe_agent.conversation.renderer import ResponseDenied, render_response
from tests.episodes.test_episode_foundation import NOW


@pytest.fixture
def response_vault():
    return ReferenceVault(
        episode_id="completion_episode", actor_id="completion_actor",
        deadline=NOW + timedelta(minutes=10), tools=[{"name": "get_user_details"}],
    )


def render(vault, text, *, purpose="answer", evidence=(), receipts=None, stop=False):
    return render_response(
        MessageCandidate(kind="message", purpose=purpose, text=text,
                         evidence_call_ids=list(evidence), stop=stop),
        vault=vault, subject_id="customer_fixture", authenticated=True,
        successful_calls=receipts or {}, now=NOW,
    )


@pytest.mark.parametrize("text", [
    "I have a question about your preferences.",
    "I've a question about the available options.",
    "Your membership has been active for a while.",
    "Could you check whether my email was updated correctly?",
    "Could you please confirm if the refund has been processed?",
    "Has your email been updated?",
    "Was the request approved?",
    "I want to ensure it's updated correctly.",
    "I understand you changed your email and want to ensure it's updated correctly.",
    "I can check whether your email has been updated.",
    "If the request is approved, the card would be issued after review.",
    "If approved, it may be processed after the required checks.",
    "Your address can be updated after the required verification.",
    "Your request would be successfully processed after approval.",
    "Has the request been successfully processed?",
    "Has your account been closed?",
    "Your card can be replaced after verification.",
])
def test_possession_status_questions_and_future_explanations_remain_usable(response_vault, text):
    assert render(response_vault, text) == text


@pytest.mark.parametrize("purpose", ["question", "answer", "policy", "user_instruction", "finish"])
@pytest.mark.parametrize("text", [
    "I have transferred the funds.",
    "Your refund has been processed.",
    "Your email is updated.",
    "Your request was approved.",
    "I've done it.",
    "I have changed your address.",
    "I have sent the payment.",
    "I have opened your account.",
    "Your account has been closed.",
    "I have scheduled the payment.",
    "I have deleted the order.",
    "We have charged your card.",
    "All done.",
])
def test_completion_claims_need_receipt_backed_host_rendering_for_every_purpose(
    response_vault, purpose, text,
):
    with pytest.raises(ResponseDenied, match="CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE"):
        render(response_vault, text, purpose=purpose)


@pytest.mark.parametrize("text", [
    "I have refunded your payment?",
    "Your refund was processed?",
    "Was your email updated? I have transferred the funds.",
    "Could you check whether your email was updated, and I have refunded the payment?",
    "Was your request approved and your refund has been processed?",
    "Could you confirm if your email is updated; your request was approved.",
    "If your request is approved, your refund has been processed.",
    "Your address can be updated, but I have already refunded the payment.",
    "You said your email has been updated; your refund was processed.",
    "According to your message, I have transferred the funds.",
    'The customer said "I have refunded the payment".',
    "Was your email updated I have transferred the funds?",
    "Could you check whether your email is updated your refund has been processed?",
])
def test_questions_conditions_attribution_and_mixed_clauses_do_not_hide_claims(response_vault, text):
    with pytest.raises(ResponseDenied, match="CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE"):
        render(response_vault, text, purpose="question")


@pytest.mark.parametrize("text", [
    "Has your account been closed? Yes.",
    "Was your email updated? It was.",
    "Has your email been updated? It has.",
    "Were the requests approved? They were.",
    "Was your email updated? That's correct.",
    "Was your email updated? Absolutely it was.",
    "Please verify that your card was approved; yes, it was.",
    "Could you check whether your email was updated? Certainly, it was.",
    "Was your email updated yes it was?",
    "Could you check whether your email was updated correctly yes it was?",
])
def test_status_questions_cannot_assert_completion_through_an_affirmative_tail(response_vault, text):
    with pytest.raises(ResponseDenied, match="CRITICAL_CLAIM_REQUIRES_SUCCESS_TEMPLATE"):
        render(response_vault, text, purpose="answer")


@pytest.mark.parametrize("text", [
    "Has your account been closed? Please check your records.",
    "Was your email updated? I can help with the next question.",
    "Could you check whether your email was updated? Yes, I can check that.",
    "Please verify that your email was updated. Certainly, I can help with verification.",
    "Has your account been closed? Not yet.",
    "Was your email updated? Your request is still pending.",
    "Was your email updated? Would you like help with verification? Yes.",
    "If approved, your card would be issued. Yes, I can explain the next steps.",
])
def test_status_questions_keep_unrelated_statements_and_check_acknowledgements(response_vault, text):
    assert render(response_vault, text) == text


def test_all_cited_receipts_are_checked_before_success_or_benign_wording(response_vault):
    receipts = {"current_call": {"effect": "business_write", "tool_name": "update_address"}}
    for purpose, text in (("success", "Invented amount and status"),
                          ("question", "Could you check whether your email was updated?")):
        with pytest.raises(ResponseDenied, match="RESPONSE_EVIDENCE_NOT_FOUND"):
            render(response_vault, text, purpose=purpose,
                   evidence=("current_call", "foreign_episode_call"), receipts=receipts)


@pytest.mark.parametrize("receipts,evidence,reason", [
    ({}, (), "SUCCESS_REQUIRES_TOOL_RESULT"),
    ({}, ("missing_call",), "RESPONSE_EVIDENCE_NOT_FOUND"),
    ({"read_call": {"effect": "read", "dispatch_effect": "write", "tool_name": "get_balance"}},
     ("read_call",), "SUCCESS_REQUIRES_MUTATION_RESULT"),
])
def test_missing_or_read_only_evidence_cannot_authorize_success(
    response_vault, receipts, evidence, reason,
):
    with pytest.raises(ResponseDenied, match=reason):
        render(response_vault, "A model claim", purpose="success", evidence=evidence,
               receipts=receipts)


def test_success_text_is_host_rendered_and_model_claim_text_is_discarded(response_vault):
    receipts = {"write_call": {"effect": "business_write", "tool_name": "update_address"}}
    result = render(response_vault, "Invented balance and invented transaction status",
                    purpose="success", evidence=("write_call",), receipts=receipts)
    assert result == "The tool confirmed the requested action: update address."
    with pytest.raises(ResponseDenied, match="STOP_REQUIRES_FINISH_INTENT"):
        render(response_vault, "Anything", purpose="success", evidence=("write_call",),
               receipts=receipts, stop=True)


@pytest.mark.parametrize("case", ["secret", "foreign_subject", "unknown_reference"])
def test_benign_language_still_goes_through_private_rendering(response_vault, case):
    if case == "secret":
        reference = response_vault.reference("1234", kind="pin", source="customer",
                                             subject_id="customer_fixture")
    elif case == "foreign_subject":
        reference = response_vault.reference("other@example.test", kind="email", source="tool",
                                             subject_id="other_customer")
    else:
        reference = "[[safe:email:" + "0" * 32 + "]]"
    with pytest.raises(ProjectionDenied):
        render(response_vault, "I have a question about " + reference + ".")


def test_failed_native_result_cannot_supply_a_success_receipt(tmp_path, monkeypatch):
    # This exercises the actual receipt producer as well as the renderer. A
    # caller-supplied 'successful_calls' fixture must not manufacture a failed
    # result into a successful host record.
    from tests.episodes.test_episode_runtime_audit import address_fixture, customer

    runtime, transport, state, _, engine = address_fixture(tmp_path)
    try:
        call, state = runtime.step(customer("Yes.", "confirm_fixture"), state)
        feedback = []

        def propose(request):
            projected = request.projected_context
            assert projected["successful_calls"] == {}
            feedback.append(projected["feedback"])
            if projected["feedback"] is None:
                return json.dumps({"kind": "message", "purpose": "success", "text": "all done",
                                   "evidence_call_ids": [call.call_id]})
            return json.dumps({"kind": "message", "purpose": "question",
                               "text": "Would you like to review the request?"})

        monkeypatch.setattr(transport, "_outputs", propose)
        decision, _ = runtime.step(Observation(observation_id="failed_result_fixture", messages=[
            Message(role="tool", call_id=call.call_id, tool_name=call.tool_name,
                    content="Error: update rejected", error=True),
        ]), state)
        assert runtime.episodes.get_call("ep_audit", call.call_id).success is False
        assert feedback == [None, "RESPONSE_EVIDENCE_NOT_FOUND"]
        assert decision.content == "Would you like to review the request?"
    finally:
        engine.dispose()
