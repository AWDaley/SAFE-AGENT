"""Trusted billing-source adapter boundary; provider readers stay verifier-side."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from safe_agent.billing.adapters import BillingEvidenceAdapter
from safe_agent.billing_service.service import (
    TrustedBillingVerifierPolicy,
    TrustedBillingWorkloadAuthenticator,
    create_billing_verifier_service,
)
from safe_agent.canonical import sha256
from safe_agent.contracts.billing_verification import NormalizedBillingEvidence
from tests.m2.conftest import NOW
from tests.m7.test_asymmetric_billing_verifier import ISSUER, KEY_ID, PRIVATE

HOST = "billing.example.com"
WORKLOAD = SecretStr("provider_adapter_" + "w" * 48)


class FixtureProviderAdapter(BillingEvidenceAdapter):
    def __init__(self, *, observed_at=NOW) -> None:
        self._observed_at = observed_at

    @property
    def adapter_id(self) -> str:
        return "fixture_provider_export_v1"

    def normalize(self, payload: str, *, now) -> NormalizedBillingEvidence:
        parsed = json.loads(payload)
        if parsed != {"provider_record": "fixture_123", "charge_microusd": 41}:
            raise ValueError("fixture provider record denied")
        return NormalizedBillingEvidence(
            source_adapter_id=self.adapter_id,
            source_record_id="provider_record_fixture_123",
            request_id="inf_" + "a" * 32,
            organization_id="org_fixture",
            task_id="task_fixture",
            route_id="route_fixture",
            model_revision="model_fixture_v1",
            prompt_digest="sha256:" + "b" * 64,
            charge_microusd=41,
            source_evidence_hash=sha256(parsed),
            source_observed_at=self._observed_at,
        )


def app(adapter):
    return create_billing_verifier_service(
        TrustedBillingVerifierPolicy(
            expected_host=HOST, issuer_id=ISSUER, verifier_key_id=KEY_ID,
            max_evidence_age_seconds=3600,
        ),
        TrustedBillingWorkloadAuthenticator(WORKLOAD),
        adapter=adapter,
        issuer_private_key=PRIVATE,
        clock=lambda: NOW + timedelta(minutes=2),
    )


def headers():
    return {
        "Host": HOST,
        "Authorization": "Bearer " + WORKLOAD.get_secret_value(),
        "Content-Type": "application/json",
    }


def test_provider_adapter_normalizes_before_signing_without_business_authority():
    client = TestClient(app(FixtureProviderAdapter()))
    response = client.post(
        "/v1/verify", headers=headers(),
        json={"provider_record": "fixture_123", "charge_microusd": 41},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_adapter_id"] == "fixture_provider_export_v1"
    assert body["charge_microusd"] == 41
    assert body["signature_algorithm"] == "ed25519"
    for forbidden in ("provider_api_key", "oauth", "execute", "approve", "tools"):
        assert forbidden not in response.text


@pytest.mark.parametrize("observed", [
    NOW - timedelta(hours=2),
    NOW + timedelta(hours=1),
])
def test_service_rejects_stale_or_future_normalized_evidence_even_if_adapter_returns_it(observed):
    client = TestClient(app(FixtureProviderAdapter(observed_at=observed)))
    response = client.post(
        "/v1/verify", headers=headers(),
        json={"provider_record": "fixture_123", "charge_microusd": 41},
    )
    assert response.status_code == 409
    assert not response.content


def test_unreviewed_object_cannot_be_used_as_provider_adapter():
    class NotAnAdapter:
        pass

    with pytest.raises(TypeError):
        app(NotAnAdapter())
