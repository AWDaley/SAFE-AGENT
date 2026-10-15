"""M7-04 independent synthetic-source proof; never a live provider invoice."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from safe_agent.billing import (
    BillingSourceDenied,
    OfflineSyntheticBillingVerifier,
    SyntheticBillingSourceRecord,
)
from safe_agent.canonical import canonical_json
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from safe_agent.storage.models import InferenceBillingAttestationRow
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import setup
from tests.m6.test_synthetic_workflow import new_task
from tests.m7.test_inference_budget import limits, make_request

SOURCE_KEY = b"M7_SYNTHETIC_external_source_key_ONLY_for_offline_test_v1"
ISSUER_KEY = b"M7_SEPARATE_verifier_HMAC_key_ONLY_for_offline_test_v1"


def source_record(request, *, amount=80, source_id="a", key=SOURCE_KEY,
                  issued=None):
    payload = {
        "source_kind": "synthetic_offline_only",
        "source_record_id": "src_" + source_id * 32,
        "request_id": request.request_id,
        "organization_id": request.task.organization_id,
        "task_id": request.task.task_id,
        "route_id": request.route_id,
        "model_revision": request.model_revision,
        "prompt_digest": request.prompt_digest,
        "charge_microusd": amount,
        "issued_at": issued or NOW + timedelta(minutes=1),
        "source_signature": "0" * 64,
    }
    initial = SyntheticBillingSourceRecord(**payload)
    signed = hmac.new(
        key, canonical_json(initial.model_dump(
            mode="json", exclude={"source_signature"},
        )), hashlib.sha256,
    ).hexdigest()
    return initial.model_copy(update={"source_signature": signed})


def verifier():
    return OfflineSyntheticBillingVerifier(
        source_key=SOURCE_KEY, issuer_key=ISSUER_KEY,
        issuer_id="independent_offline_billing_v1",
    )


def test_two_independent_keys_required_and_no_provider_source_accepted():
    with pytest.raises(ValueError):
        OfflineSyntheticBillingVerifier(
            source_key=SOURCE_KEY, issuer_key=SOURCE_KEY,
            issuer_id="same_key_forbidden",
        )
    with pytest.raises(ValueError):
        OfflineSyntheticBillingVerifier(
            source_key=b"short", issuer_key=ISSUER_KEY,
            issuer_id="too_short",
        )
    with pytest.raises(ValidationError):
        SyntheticBillingSourceRecord(
            source_record_id="src_" + "a" * 32, request_id="inf_" + "b" * 32,
            organization_id="org_demo", task_id="task_demo",
            route_id="r", model_revision="m",
            prompt_digest="sha256:" + "d" * 64,
            charge_microusd=99,
            source_kind="provider_reported",
            source_signature="f" * 64, issued_at=NOW,
        )


def test_attestation_integrates_with_ledger_without_real_invoice(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=500, synthetic_mock=False, now=NOW)

    synthetic_statement = source_record(req, amount=80)
    attestation = verifier().attest(
        synthetic_statement, now=NOW + timedelta(minutes=2),
    )
    assert attestation.source_evidence_hash.startswith("sha256:")
    assert attestation.issuer_id == "independent_offline_billing_v1"
    assert attestation.charge_microusd == 80
    result = ledger.reconcile_attested(
        attestation, verifier_secret=ISSUER_KEY,
        now=NOW + timedelta(minutes=3),
    )
    assert result.status == "RECONCILED"
    assert result.charged_microusd == 80
    assert result.held_microusd == 0
    assert result.effect_authorized is False
    assert result.usage_independently_verified is False
    assert ledger.view(employee, task_id, now=NOW).task.available_microusd == 1120
    assert kit["repo"].replay_task(task_id).external_effects == 0
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            attestation, verifier_secret=ISSUER_KEY,
            now=NOW + timedelta(minutes=4),
        )
    with kit["repo"]._sessions() as db:
        assert len(db.scalars(select(InferenceBillingAttestationRow)).all()) == 1


@pytest.mark.parametrize("alter", [
    {"charge_microusd": 1},
    {"organization_id": "org_other"},
    {"model_revision": "other_model"},
    {"source_signature": "0" * 64},
    {"task_id": "forged_task"},
])
def test_bad_or_tampered_source_cannot_issue_billing_attestation(kit, alter):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    statement = source_record(req)
    forged = statement.model_copy(update=alter)
    with pytest.raises(BillingSourceDenied) as err:
        verifier().attest(forged, now=NOW + timedelta(minutes=2))
    assert str(err.value) == "SYNTHETIC_BILLING_SOURCE_DENIED"
    # The billing source did not write anything to the invoice ledger.
    with kit["repo"]._sessions() as db:
        assert db.scalars(select(InferenceBillingAttestationRow)).all() == []


def test_stale_and_future_source_proof_refused(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    with pytest.raises(BillingSourceDenied):
        verifier().attest(
            source_record(req, issued=NOW - timedelta(hours=2)),
            now=NOW,
        )
    with pytest.raises(BillingSourceDenied):
        verifier().attest(
            source_record(req, issued=NOW + timedelta(hours=2)),
            now=NOW,
        )


def test_source_signature_not_a_customer_visible_execution_capability(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    record = source_record(req)
    att = verifier().attest(record, now=NOW + timedelta(minutes=2))
    for private in ("api_key", "oauth", "cal_main_private", "cust_c1",
                    "google_credentials"):
        assert private not in att.model_dump_json()
    assert "source_signature" not in att.model_dump_json()
    assert att.receipt_id.startswith("bil_")
