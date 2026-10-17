"""Asymmetric billing verifier: private signing key stays outside SAFE-AGENT reconciliation."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from safe_agent.billing.verifier import OfflineAsymmetricBillingVerifier
from safe_agent.billing_service.service import (
    TrustedBillingVerifierPolicy,
    TrustedBillingWorkloadAuthenticator,
    create_billing_verifier_service,
)
from safe_agent.canonical import canonical_json
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from safe_agent.storage.models import InferenceBillingAttestationRow, InferenceBudgetReservationRow
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import setup
from tests.m6.test_synthetic_workflow import new_task
from tests.m7.test_inference_budget import limits, make_request
from tests.m7.test_offline_billing_verifier import SOURCE_KEY, source_record

WORKLOAD = SecretStr("billing_workload_" + "w" * 48)
HOST = "billing.example.com"
ISSUER = "independent_offline_billing_ed25519_v1"
KEY_ID = "billing_ed25519_fixture_v1"
PRIVATE = bytes(range(1, 33))


def public_key() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(PRIVATE).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )


def service(*, source_key=SOURCE_KEY, private=PRIVATE):
    app = create_billing_verifier_service(
        TrustedBillingVerifierPolicy(
            expected_host=HOST, issuer_id=ISSUER, verifier_key_id=KEY_ID,
        ),
        TrustedBillingWorkloadAuthenticator(WORKLOAD),
        source_key=source_key,
        issuer_private_key=private,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    return TestClient(app)


def headers():
    return {
        "Host": HOST,
        "Authorization": "Bearer " + WORKLOAD.get_secret_value(),
        "Content-Type": "application/json",
    }


def issued_attestation(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    request, _ = make_request(kit, task_id)
    record = source_record(request, amount=80)
    response = service().post("/v1/verify", headers=headers(), content=record.model_dump_json())
    assert response.status_code == 200, response.text
    return employee, task_id, request, record, VerifiedBillingAttestation.model_validate_json(
        response.text,
    )


def test_verifier_service_has_no_business_routes_and_signs_asymmetrically(kit):
    _, _, _, record, attestation = issued_attestation(kit)
    assert attestation.issuer_id == ISSUER
    assert attestation.verifier_key_id == KEY_ID
    assert attestation.signature_algorithm == "ed25519"
    assert len(attestation.signature) == 128
    payload = canonical_json(attestation.model_dump(mode="json", exclude={"signature"}))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(public_key()).verify(
        bytes.fromhex(attestation.signature), payload,
    )
    for secret in (
        WORKLOAD.get_secret_value(), SOURCE_KEY.hex(), PRIVATE.hex(), record.source_signature,
    ):
        assert secret not in attestation.model_dump_json()
    client = service()
    assert client.get("/healthz").json()["real_invoice_reader_enabled"] is False
    for path in ("/v1/tasks", "/v1/infer", "/v1/dispatch", "/v1/google/events", "/docs"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("failure", [
    "missing_auth", "wrong_host", "bad_source_signature", "duplicate_key", "wrong_content_type",
])
def test_forged_source_or_caller_cannot_obtain_attestation(kit, failure):
    _, _, request, record, _ = issued_attestation(kit)
    client = service()
    request_headers = headers()
    data = record.model_dump_json()
    if failure == "missing_auth":
        request_headers.pop("Authorization")
    elif failure == "wrong_host":
        request_headers["Host"] = "attacker.example.com"
    elif failure == "bad_source_signature":
        data = record.model_copy(update={"source_signature": "0" * 64}).model_dump_json()
    elif failure == "duplicate_key":
        data = data[:-1] + ',"charge_microusd":1}'
    else:
        request_headers["Content-Type"] = "text/plain"
    response = client.post("/v1/verify", headers=request_headers, content=data)
    assert response.status_code in (400, 401, 409, 415)
    assert not response.content
    assert request.request_id not in response.text


def test_public_key_only_reconciliation_updates_accounting_not_task_effects(kit):
    employee, task_id, request, _, attestation = issued_attestation(kit)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    before = ledger.reserve(request, max_microusd=500, synthetic_mock=False, now=NOW)
    assert before.held_microusd == 500
    result = ledger.reconcile_verified(
        attestation,
        verifier_public_key=public_key(),
        expected_issuer_id=ISSUER,
        expected_verifier_key_id=KEY_ID,
        now=NOW + timedelta(minutes=3),
    )
    assert result.status == "RECONCILED"
    assert result.held_microusd == 0
    assert result.charged_microusd == 80
    assert result.effect_authorized is False
    assert kit["repo"].replay_task(task_id).external_effects == 0
    assert ledger.view(employee, task_id, now=NOW).task.available_microusd == 1120
    with kit["repo"]._sessions() as db:
        evidence = db.scalars(select(InferenceBillingAttestationRow)).one()
        reservation = db.get(InferenceBudgetReservationRow, request.request_id)
        assert evidence.attestation_digest == reservation.evidence_hash
        assert evidence.issuer_id == ISSUER


@pytest.mark.parametrize("change", [
    "wrong_public_key", "wrong_issuer", "wrong_key_id", "tampered_charge", "tampered_signature",
])
def test_reconciliation_fails_closed_without_verifier_private_key(kit, change):
    _, _, request, _, attestation = issued_attestation(kit)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(request, max_microusd=500, synthetic_mock=False, now=NOW)
    key = public_key()
    issuer, key_id = ISSUER, KEY_ID
    evidence = attestation
    if change == "wrong_public_key":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
    elif change == "wrong_issuer":
        issuer = "different_billing_verifier"
    elif change == "wrong_key_id":
        key_id = "different_billing_key"
    elif change == "tampered_charge":
        evidence = evidence.model_copy(update={"charge_microusd": 1})
    else:
        evidence = evidence.model_copy(update={"signature": "0" * 128})
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_verified(
            evidence, verifier_public_key=key,
            expected_issuer_id=issuer, expected_verifier_key_id=key_id,
            now=NOW + timedelta(minutes=3),
        )
    with kit["repo"]._sessions() as db:
        reservation = db.get(InferenceBudgetReservationRow, request.request_id)
        assert reservation.status == "IN_FLIGHT"
        assert reservation.held_microusd == 500
        assert db.scalars(select(InferenceBillingAttestationRow)).all() == []


def test_asymmetric_verifier_requires_separate_source_and_private_keys():
    with pytest.raises(ValueError):
        OfflineAsymmetricBillingVerifier(
            source_key=PRIVATE, issuer_private_key=PRIVATE,
            issuer_id=ISSUER, verifier_key_id=KEY_ID,
        )
    with pytest.raises(ValueError):
        OfflineAsymmetricBillingVerifier(
            source_key=SOURCE_KEY, issuer_private_key=b"short",
            issuer_id=ISSUER, verifier_key_id=KEY_ID,
        )
