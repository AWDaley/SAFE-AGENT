"""OpenAI organization Costs API aggregate-audit adapter acceptance."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Inexact, Rounded, localcontext

import pytest
from fastapi.testclient import TestClient

from safe_agent.billing.adapters import SyntheticHMACBillingAdapter
from safe_agent.billing.openai_costs import OpenAIOrganizationCostsAdapter
from safe_agent.billing.verifier import BillingSourceDenied
from safe_agent.billing_service.service import (
    TrustedBillingVerifierPolicy,
    TrustedBillingWorkloadAuthenticator,
    create_billing_verifier_service,
)
from safe_agent.canonical import canonical_json
from safe_agent.contracts.provider_billing_audit import VerifiedProviderCostAudit
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import setup
from tests.m6.test_synthetic_workflow import new_task
from tests.m7.test_asymmetric_billing_verifier import (
    HOST,
    ISSUER,
    KEY_ID,
    PRIVATE,
    WORKLOAD,
    headers,
    keyring,
    public_key,
)
from tests.m7.test_inference_budget import limits, make_request
from tests.m7.test_offline_billing_verifier import SOURCE_KEY

START = int(datetime(2026, 10, 14, tzinfo=UTC).timestamp())
DAY = 86_400


def capture() -> dict[str, object]:
    return {
        "schema_version": "safe_agent.openai_costs_capture.v1",
        "provider": "openai",
        "endpoint": "/v1/organization/costs",
        "start_time": START,
        "end_time": START + 2 * DAY,
        "bucket_width": "1d",
        "group_by": ["project_id", "line_item"],
        "fetched_at": (NOW + timedelta(minutes=1)).isoformat(),
        "pages": [
            {
                "request_page": None,
                "response": {
                    "object": "page",
                    "data": [{
                        "object": "bucket",
                        "start_time": START,
                        "end_time": START + DAY,
                        "results": [
                            {
                                "object": "organization.costs.result",
                                "amount": {"value": 0.06, "currency": "usd"},
                                "line_item": "Text models",
                                "project_id": "proj_alpha",
                            },
                            {
                                "object": "organization.costs.result",
                                "amount": {"value": 0.04, "currency": "usd"},
                                "line_item": "Text models",
                                "project_id": "proj_beta",
                            },
                        ],
                    }],
                    "has_more": True,
                    "next_page": "page_costs_2",
                },
            },
            {
                "request_page": "page_costs_2",
                "response": {
                    "object": "page",
                    "data": [{
                        "object": "bucket",
                        "start_time": START + DAY,
                        "end_time": START + 2 * DAY,
                        "results": [{
                            "object": "organization.costs.result",
                            "amount": {"value": 0.000001, "currency": "usd"},
                            "line_item": "Other",
                            "project_id": "proj_alpha",
                        }],
                    }],
                    "has_more": False,
                    "next_page": None,
                },
            },
        ],
    }


def payload(value: dict[str, object] | None = None) -> str:
    return json.dumps(value or capture(), separators=(",", ":"), sort_keys=True)


def exact_amount_payload(*tokens: str) -> str:
    """Insert authored JSON number/string tokens without a Python float round trip."""
    value = capture()
    pages = value["pages"]
    assert isinstance(pages, list)
    first = pages[0]["response"]["data"][0]
    template = first["results"][0]
    first["results"] = [
        {**template, "amount": {"value": f"exact_amount_{index}", "currency": "usd"}}
        for index in range(len(tokens))
    ]
    pages[1]["response"]["data"][0]["results"] = []
    result = payload(value)
    for index, token in enumerate(tokens):
        result = result.replace(json.dumps(f"exact_amount_{index}"), token)
    return result


def service() -> TestClient:
    app = create_billing_verifier_service(
        TrustedBillingVerifierPolicy(
            expected_host=HOST, issuer_id=ISSUER, verifier_key_id=KEY_ID,
        ),
        TrustedBillingWorkloadAuthenticator(WORKLOAD),
        adapter=SyntheticHMACBillingAdapter(source_key=SOURCE_KEY),
        audit_adapter=OpenAIOrganizationCostsAdapter(),
        issuer_private_key=PRIVATE,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    return TestClient(app)


def test_openai_cost_capture_normalizes_complete_aggregate_without_request_authority():
    result = OpenAIOrganizationCostsAdapter().normalize_audit(
        payload(), now=NOW + timedelta(minutes=2),
    )
    assert result.provider == "openai"
    assert result.source_adapter_id == "openai_organization_costs_v1"
    assert result.total_usd_decimal == "0.100001"
    assert result.bucket_count == 2
    assert result.result_count == 3
    assert result.project_ids == ("proj_alpha", "proj_beta")
    assert result.line_items == ("Other", "Text models")
    assert result.request_reconciliation_supported is False
    assert result.request_reconciliation_block_reason == "PROVIDER_COSTS_ARE_AGGREGATED"


def test_verifier_signs_openai_cost_audit_but_keeps_request_reconciliation_disabled():
    client = service()
    health = client.get("/healthz").json()
    assert health["provider_cost_audit_enabled"] is True
    assert health["request_level_provider_reconciliation_enabled"] is False
    assert health["real_invoice_reader_enabled"] is False

    response = client.post(
        "/v1/audit/provider-costs", headers=headers(), content=payload(),
    )
    assert response.status_code == 200, response.text
    evidence = VerifiedProviderCostAudit.model_validate_json(response.text)
    assert evidence.audit.provider == "openai"
    assert evidence.audit.request_reconciliation_supported is False
    unsigned = canonical_json(evidence.model_dump(mode="json", exclude={"signature"}))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(public_key()).verify(
        bytes.fromhex(evidence.signature), unsigned,
    )


@pytest.mark.parametrize("mutation", [
    "incomplete_last_page",
    "broken_cursor",
    "bucket_gap",
    "wrong_currency",
    "misaligned_window",
    "duplicate_bucket",
])
def test_openai_cost_capture_fails_closed_on_incomplete_or_ambiguous_evidence(mutation):
    value = capture()
    pages = value["pages"]
    assert isinstance(pages, list)
    first = pages[0]
    second = pages[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    first_response = first["response"]
    second_response = second["response"]
    assert isinstance(first_response, dict) and isinstance(second_response, dict)

    if mutation == "incomplete_last_page":
        second_response["has_more"] = True
        second_response["next_page"] = "page_costs_3"
    elif mutation == "broken_cursor":
        second["request_page"] = "different_page"
    elif mutation == "bucket_gap":
        second_response["data"][0]["start_time"] = START + 2 * DAY
        second_response["data"][0]["end_time"] = START + 3 * DAY
    elif mutation == "wrong_currency":
        first_response["data"][0]["results"][0]["amount"]["currency"] = "eur"
    elif mutation == "misaligned_window":
        value["start_time"] = START + 1
    else:
        second_response["data"][0]["start_time"] = START
        second_response["data"][0]["end_time"] = START + DAY

    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            payload(value), now=NOW + timedelta(minutes=2),
        )


def test_openai_cost_precision_is_preserved_without_rounding():
    value = capture()
    pages = value["pages"]
    assert isinstance(pages, list)
    first_response = pages[0]["response"]
    first_response["data"][0]["results"][0]["amount"]["value"] = 0.0000000000001
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            payload(value), now=NOW + timedelta(minutes=2),
        )


@pytest.mark.parametrize("token,expected", [
    ("9007199254740993", "9007199254740993"),
    ("9007199254740993.123456789012", "9007199254740993.123456789012"),
    ('"9007199254740993.123456789012"', "9007199254740993.123456789012"),
    ("0.000000000001", "0.000000000001"),
    ('"1e-12"', "0.000000000001"),
    ("1.0000000000000000", "1"),
    ('"1.0000000000000000"', "1"),
])
def test_exact_json_amounts_preserve_all_supported_decimal_digits(token, expected):
    result = OpenAIOrganizationCostsAdapter().normalize_audit(
        exact_amount_payload(token), now=NOW + timedelta(minutes=2),
    )
    assert result.total_usd_decimal == expected


@pytest.mark.parametrize("tokens", [
    ("1.0000000000000001",),
    ('"1.0000000000000001"',),
    ("9999999999999999.0000000000001",),
    ('"9999999999999999.0000000000001"',),
    ("1e-13",),
    ("0.0000000000004", "0.0000000000006"),
    ('"0.0000000000004"', '"0.0000000000006"'),
    ("1e1000000000",),
    ("1e-1000000000",),
    ("9999999999999999.999999999999", "0.000000000001"),
])
def test_unsupported_input_precision_or_aggregate_size_is_never_rounded_into_range(tokens):
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            exact_amount_payload(*tokens), now=NOW + timedelta(minutes=2),
        )


def test_cost_sum_does_not_depend_on_decimal_context_or_suppress_rounding_errors():
    with localcontext() as context:
        context.prec = 6
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = OpenAIOrganizationCostsAdapter().normalize_audit(
            exact_amount_payload("1234567890123456.123456789012", "0.000000000001"),
            now=NOW + timedelta(minutes=2),
        )
    assert result.total_usd_decimal == "1234567890123456.123456789013"


def test_distinct_exact_amounts_never_share_rounded_audit_evidence():
    adapter = OpenAIOrganizationCostsAdapter()
    first = adapter.normalize_audit(
        exact_amount_payload("9007199254740993.000000000001"),
        now=NOW + timedelta(minutes=2),
    )
    second = adapter.normalize_audit(
        exact_amount_payload("9007199254740993.000000000002"),
        now=NOW + timedelta(minutes=2),
    )
    assert first.total_usd_decimal == "9007199254740993.000000000001"
    assert second.total_usd_decimal == "9007199254740993.000000000002"
    assert first.source_evidence_hash != second.source_evidence_hash


@pytest.mark.parametrize("token", [
    "NaN", "Infinity", "-Infinity", '"NaN"', '"Infinity"', '"-Infinity"',
    "true", "false", "null", "[]", "{}", "-1", '"-0.000000000001"',
])
def test_invalid_or_nonfinite_cost_values_still_fail_closed(token):
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            exact_amount_payload(token), now=NOW + timedelta(minutes=2),
        )


@pytest.mark.parametrize("field,value", [
    ("fetched_at", "2026-10-14T13:00:00"),
    ("fetched_at", START),
    ("fetched_at", True),
    ("start_time", str(START)),
    ("start_time", float(START)),
    ("start_time", True),
    ("unexpected", "extra field"),
])
def test_exact_cost_parsing_preserves_strict_metadata_validation(field, value):
    malformed = capture()
    malformed[field] = value
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            payload(malformed), now=NOW + timedelta(minutes=2),
        )


def test_duplicate_cost_keys_are_rejected_before_decimal_validation():
    malformed = exact_amount_payload("1").replace('"value":1', '"value":1,"value":2')
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            malformed, now=NOW + timedelta(minutes=2),
        )


def test_aggregate_cost_audit_cannot_reconcile_or_release_one_request(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task = new_task(client, signer, employee)
    request, _ = make_request(kit, task)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    before = ledger.reserve(request, max_microusd=500, synthetic_mock=False, now=NOW)

    response = service().post(
        "/v1/audit/provider-costs", headers=headers(), content=payload(),
    )
    evidence = VerifiedProviderCostAudit.model_validate_json(response.text)
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_verified(
            evidence,  # type: ignore[arg-type]
            keyring=keyring(),
            now=NOW + timedelta(minutes=3),
        )
    after = ledger.receipt(employee, request.request_id, now=NOW + timedelta(minutes=3))
    assert after.status == before.status == "IN_FLIGHT"
    assert after.held_microusd == before.held_microusd == 500
    assert after.charged_microusd == 0


def test_usage_or_request_shaped_payload_is_not_accepted_as_cost_evidence():
    usage = {
        "object": "response",
        "id": "resp_123",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    with pytest.raises(BillingSourceDenied):
        OpenAIOrganizationCostsAdapter().normalize_audit(
            payload(usage), now=NOW + timedelta(minutes=2),
        )
