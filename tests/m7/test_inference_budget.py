"""M7-03: durable, atomic global/tenant/task inference cost admission.

All examples use synthetic data and the offline M7 mock. They do not
contact an inference provider, use customer credentials, or verify a bill.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.inference import (
    InferenceReportedUsage,
    InferenceRequest,
    InferenceRoutePolicy,
)
from safe_agent.contracts.inference_budget import (
    BillingChargeAttestation,
    InferenceBudgetLimits,
)
from safe_agent.contracts.office import AvailableSlot, Projection
from safe_agent.inference import InferenceDenied, InferenceGateway, OfflineMockInferenceTransport
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from safe_agent.storage.models import (
    InferenceBillingAttestationRow,
    InferenceBudgetEventRow,
    InferenceBudgetReservationRow,
)
from safe_agent.storage.repository import LedgerRepository, StateConflict
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import setup
from tests.m6.test_synthetic_workflow import new_task
from tests.m6.test_task_intake import auth
from tests.m7.test_inference_gateway import fixture


BILLING_KEY = b"M7_separate_trusted_billing_verifier_secret_not_for_live_00000001"


def limits(*, global_limit=2000, tenant_limit=1500,
           task_limit=1200, per_call=1000):
    return InferenceBudgetLimits(
        global_microusd=global_limit,
        tenant_microusd=tenant_limit,
        task_microusd=task_limit,
        max_call_microusd=per_call,
    )


def make_request(kit, task_id, *, key="a", issued=NOW):
    with kit["repo"].write_task(task_id) as tx:
        manifest = tx.manifest
        task = tx.task
    duration = kit["office"].trusted_template_duration(
        manifest.template_id, organization_id=task.organization_id,
    )
    context = SafeTaskContext(
        task_id=task_id, organization_id=task.organization_id,
        calendar_ref=manifest.calendar_ref, customer_ref=manifest.customer_ref,
        allowed_start=manifest.allowed_start, allowed_end=manifest.allowed_end,
        duration_minutes=duration,
        time_zone=manifest.time_zone, template_id=manifest.template_id,
        simulation_ref="simulation_pending",
    )
    availability = Projection(
        organization_id=context.organization_id, task_id=task_id,
        calendar_ref=context.calendar_ref, revision="budget_projection_" + key,
        fetched_at=issued, expires_at=issued + timedelta(minutes=5),
        free_slots=(AvailableSlot(
            start_at=manifest.allowed_start, end_at=manifest.allowed_end,
        ),),
    )
    digest = sha256({
        "instruction": PLANNER_INSTRUCTION,
        "task": context, "availability": availability,
    })
    request = InferenceRequest(
        request_id="inf_" + key * 32, route_id="budget_route",
        model_revision="budget_model_v1", prompt_digest=digest,
        issued_at=issued, deadline_at=issued + timedelta(seconds=10),
        task=context, availability=availability,
    )
    prompt = canonical_json({
        "instruction": PLANNER_INSTRUCTION,
        "task": context, "availability": availability,
    }).decode("utf-8")
    return request, prompt


def sign_evidence(req, *, amount=125, receipt="c",
                  secret=BILLING_KEY, source_digest=None):
    payload = {
        "schema_version": "safe_agent.billing_charge_attestation.v1",
        "receipt_id": "bil_" + receipt * 32,
        "request_id": req.request_id,
        "organization_id": req.task.organization_id,
        "task_id": req.task.task_id,
        "route_id": req.route_id,
        "model_revision": req.model_revision,
        "prompt_digest": req.prompt_digest,
        "charge_microusd": amount,
        "source_evidence_hash": source_digest or sha256({
            "provider": "synthetic_attestation", "request": req.request_id,
        }),
        "issuer_id": "independent_billing_verifier_test",
        "observed_at": NOW + timedelta(minutes=1),
    }
    unsigned = BillingChargeAttestation(
        **payload, signature="0" * 64,
    )
    signature = hmac.new(
        secret,
        canonical_json(unsigned.model_dump(mode="json", exclude={"signature"})),
        hashlib.sha256,
    ).hexdigest()
    return unsigned.model_copy(update={"signature": signature})


def test_strict_limits_and_no_execution_or_secret_fields():
    with pytest.raises(ValidationError):
        InferenceBudgetLimits(global_microusd=-1, tenant_microusd=0,
                              task_microusd=0, max_call_microusd=0)
    with pytest.raises(ValidationError):
        InferenceBudgetLimits(
            global_microusd=500, tenant_microusd=400,
            task_microusd=300, max_call_microusd=200,
            provider_api_key="do_not_store",
        )
    with pytest.raises(ValidationError):
        BillingChargeAttestation(
            receipt_id="bil_" + "a" * 32, request_id="inf_" + "b" * 32,
            organization_id="org_demo", task_id="task_demo",
            route_id="r", model_revision="m",
            prompt_digest="sha256:" + "f" * 64,
            charge_microusd=-1,
            source_evidence_hash="sha256:" + "b" * 64,
            issuer_id="audit", observed_at=NOW, signature="e" * 64,
        )


def test_reservation_holds_all_three_accounts_and_is_not_replayed(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    first = ledger.reserve(req, max_microusd=600, synthetic_mock=False, now=NOW)
    assert first.status == "IN_FLIGHT"
    assert first.held_microusd == 600 and first.charged_microusd == 0
    assert first.effect_authorized is False
    view = ledger.view(employee, task_id, now=NOW)
    assert view.task.held_microusd == 600 and view.task.available_microusd == 600
    assert view.tenant.held_microusd == 600
    assert view.global_remaining_microusd == 1400

    with pytest.raises(InferenceBudgetDenied):
        ledger.reserve(req, max_microusd=600, synthetic_mock=False, now=NOW)
    second, _ = make_request(kit, task_id, key="b")
    with pytest.raises(InferenceBudgetDenied):
        ledger.reserve(second, max_microusd=650, synthetic_mock=False, now=NOW)
    assert ledger.view(employee, task_id, now=NOW).task.held_microusd == 600
    assert len(kit["repo"].read_events(task_id)) == 1
    assert ledger.receipt(employee, req.request_id, now=NOW).ledger_hash.startswith("sha256:")


def test_tenant_limit_prevents_second_task_and_limit_cannot_change(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_a = new_task(client, signer, employee)
    task_b = new_task(
        client, signer, employee,
        params={"idempotency_key": "req_" + "d" * 32},
    )
    ledger = InferenceBudgetLedger(
        kit["repo"], limits(global_limit=4000, tenant_limit=800, task_limit=700),
    )
    first, _ = make_request(kit, task_a)
    other, _ = make_request(kit, task_b, key="b")
    ledger.reserve(first, max_microusd=500, synthetic_mock=False, now=NOW)
    with pytest.raises(InferenceBudgetDenied):
        ledger.reserve(other, max_microusd=500, synthetic_mock=False, now=NOW)
    assert ledger.view(employee, task_b, now=NOW).tenant.held_microusd == 500
    assert ledger.view(employee, task_b, now=NOW).task.held_microusd == 0

    # A restarted process with a different budget configuration must NOT
    # silently raise established spending limits.
    changed = InferenceBudgetLedger(
        kit["repo"], limits(global_limit=4000, tenant_limit=810, task_limit=700),
    )
    with pytest.raises(StateConflict):
        changed.view(employee, task_a, now=NOW)


def test_global_limit_is_shared_between_registered_tasks(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    a = new_task(client, signer, employee)
    b = new_task(
        client, signer, employee,
        params={"idempotency_key": "req_" + "e" * 32},
    )
    ledger = InferenceBudgetLedger(
        kit["repo"], limits(global_limit=800, tenant_limit=2000, task_limit=1000),
    )
    first, _ = make_request(kit, a)
    second, _ = make_request(kit, b, key="b")
    ledger.reserve(first, max_microusd=500, synthetic_mock=False, now=NOW)
    with pytest.raises(InferenceBudgetDenied):
        ledger.reserve(second, max_microusd=400, synthetic_mock=False, now=NOW)
    assert ledger.view(employee, b, now=NOW).global_remaining_microusd == 300


def test_uncertain_and_expired_claims_keep_full_capacity_on_restart(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=600, synthetic_mock=False, now=NOW)
    ledger.mark_uncertain(
        req.request_id, now=NOW + timedelta(seconds=10),
        reason="UNKNOWN_EFFECT",
        reported_usage=InferenceReportedUsage(
            input_tokens=1, output_tokens=1,
            source="provider_reported",
        ),
    )
    snap = ledger.receipt(employee, req.request_id, now=NOW)
    assert snap.status == "UNCERTAIN" and snap.held_microusd == 600
    assert snap.charged_microusd == 0
    assert snap.usage_independently_verified is False

    restarted = LedgerRepository.testing_sqlite(
        "sqlite+pysqlite:///" + str(kit["repo"].engine.url.database),
    )
    try:
        recovered = InferenceBudgetLedger(restarted, limits())
        snap2 = recovered.receipt(employee, req.request_id, now=NOW)
        assert snap2.ledger_hash == snap.ledger_hash
        assert recovered.view(employee, task_id, now=NOW).task.held_microusd == 600
        assert recovered.mark_expired_uncertain(
            now=NOW + timedelta(minutes=1),
        ) == 0
    finally:
        restarted.close()


def test_crash_before_response_remains_billed_unknown_and_never_refunds(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=700, synthetic_mock=False, now=NOW)
    assert ledger.mark_expired_uncertain(
        now=NOW + timedelta(seconds=11),
    ) == 1
    record = ledger.receipt(employee, req.request_id, now=NOW)
    assert record.status == "UNCERTAIN" and record.held_microusd == 700
    assert ledger.mark_expired_uncertain(
        now=NOW + timedelta(minutes=2),
    ) == 0


def test_hmac_attested_charge_settles_and_releases_only_unused_hold(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=800, synthetic_mock=False, now=NOW)
    evidence = sign_evidence(req, amount=125)
    settled = ledger.reconcile_attested(
        evidence, verifier_secret=BILLING_KEY, now=NOW + timedelta(minutes=2),
    )
    assert settled.status == "RECONCILED"
    assert settled.held_microusd == 0 and settled.charged_microusd == 125
    assert settled.evidence_hash.startswith("sha256:")
    balances = ledger.view(employee, task_id, now=NOW)
    assert balances.task.held_microusd == 0
    assert balances.task.charged_microusd == 125
    assert balances.task.available_microusd == 1075
    assert balances.tenant.charged_microusd == 125
    assert balances.global_remaining_microusd == 1875
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            evidence, verifier_secret=BILLING_KEY,
            now=NOW + timedelta(minutes=3),
        )
    with kit["repo"]._sessions() as db:
        records = db.scalars(select(InferenceBillingAttestationRow)).all()
        assert len(records) == 1
        assert records[0].source_evidence_hash == evidence.source_evidence_hash


def test_invalid_or_over_budget_attestations_cannot_release_funds(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=500, synthetic_mock=False, now=NOW)

    altered = sign_evidence(req, amount=100).model_copy(
        update={"charge_microusd": 1},
    )
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            altered, verifier_secret=BILLING_KEY,
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            sign_evidence(req, amount=600), verifier_secret=BILLING_KEY,
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            sign_evidence(req, amount=100, secret=b"z" * 32),
            verifier_secret=BILLING_KEY, now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(InferenceBudgetDenied):
        ledger.reconcile_attested(
            sign_evidence(req, amount=100).model_copy(update={
                "organization_id": "org_other",
            }),
            verifier_secret=BILLING_KEY, now=NOW + timedelta(minutes=2),
        )
    assert ledger.receipt(employee, req.request_id, now=NOW).held_microusd == 500
    assert ledger.view(employee, task_id, now=NOW).global_remaining_microusd == 1500


def test_tampered_hash_linked_budget_event_is_detected(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=400, synthetic_mock=False, now=NOW)
    with kit["repo"]._sessions.begin() as db:
        db.execute(
            update(InferenceBudgetEventRow)
            .where(InferenceBudgetEventRow.request_id == req.request_id)
            .values(event_hash="sha256:" + "f" * 64)
        )
    with pytest.raises(StateConflict):
        ledger.receipt(employee, req.request_id, now=NOW)
    with pytest.raises(StateConflict):
        ledger.mark_uncertain(req.request_id, now=NOW)


def test_mock_closure_requires_persisted_mock_fence(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, _ = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    ledger.reserve(req, max_microusd=500, synthetic_mock=False, now=NOW)
    with pytest.raises(InferenceBudgetDenied):
        ledger.close_synthetic(req.request_id, now=NOW, exact_offline_mock=True)
    assert ledger.view(employee, task_id, now=NOW).tenant.held_microusd == 500
    with pytest.raises(InferenceBudgetDenied):
        ledger.close_synthetic(req.request_id, now=NOW, exact_offline_mock=False)


def test_budgeted_gateway_mock_releases_without_network_or_provider_effect(kit):
    client, signer, employee, calendar, internal, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    req, prompt = make_request(kit, task_id)
    assert req.task.task_id == task_id
    ledger = InferenceBudgetLedger(kit["repo"], limits(
        global_limit=5000, tenant_limit=5000, task_limit=5000,
        per_call=5000,
    ))
    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id="budget_route", model_revision="budget_model_v1",
            max_response_bytes=1024, max_response_token_proxy=1024,
            max_estimated_cost_microusd=5000,
            input_microusd_per_million_tokens=1_000_000,
            output_microusd_per_million_tokens=1_000_000,
        ),
        OfflineMockInferenceTransport(), clock=lambda: NOW,
        budget_ledger=ledger,
    )
    result = gateway.infer(prompt)
    assert result.provider_effect_performed is False
    assert result.model_authorized_execution is False
    receipt = ledger.receipt(employee, result.request_id, now=NOW)
    assert receipt.status == "SYNTHETIC_SETTLED"
    assert receipt.held_microusd == receipt.charged_microusd == 0
    assert receipt.max_estimated_microusd > 0
    assert ledger.view(employee, task_id, now=NOW).global_remaining_microusd == 5000
    assert calendar.effect_count == internal.effect_count == 0


def test_budgeted_nonmock_transport_never_treats_reported_usage_as_verified(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    _, prompt = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(
        kit["repo"], limits(global_limit=5000, tenant_limit=5000,
                            task_limit=5000, per_call=5000),
    )
    seen = []
    class FakeRemote:
        def infer(self, request):
            seen.append(request)
            return OfflineMockInferenceTransport().infer(request)

    policy = InferenceRoutePolicy(
        route_id="budget_route", model_revision="budget_model_v1",
        max_response_bytes=1024, max_response_token_proxy=1024,
        max_estimated_cost_microusd=5000,
        input_microusd_per_million_tokens=1_000_000,
        output_microusd_per_million_tokens=1_000_000,
    )
    gw = InferenceGateway(
        policy, FakeRemote(), clock=lambda: NOW, budget_ledger=ledger,
    )
    result = gw.infer(prompt)
    assert result.reported_usage is not None
    assert result.reported_usage.independently_verified is False
    receipt = ledger.receipt(employee, result.request_id, now=NOW)
    assert receipt.status == "UNCERTAIN"
    assert receipt.held_microusd == receipt.max_estimated_microusd > 0
    assert receipt.charged_microusd == 0
    assert len(seen) == 1


def test_backend_failure_retains_reserved_cost_without_leaking_error(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    _, prompt = make_request(kit, task_id)
    ledger = InferenceBudgetLedger(
        kit["repo"], limits(global_limit=5000, tenant_limit=5000,
                            task_limit=5000, per_call=5000),
    )
    class BrokenRemote:
        def infer(self, request):
            raise RuntimeError("PRIVATE_INFERENCE_TOKEN_NEVER_PRINT")

    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id="budget_route", model_revision="budget_model_v1",
            max_response_bytes=1024, max_response_token_proxy=1024,
            max_estimated_cost_microusd=5000,
            input_microusd_per_million_tokens=1_000_000,
            output_microusd_per_million_tokens=1_000_000,
        ),
        BrokenRemote(), clock=lambda: NOW,
        budget_ledger=ledger,
    )
    with pytest.raises(InferenceDenied, match="INFERENCE_BACKEND_UNAVAILABLE") as error:
        gateway.infer(prompt)
    assert "PRIVATE_INFERENCE" not in str(error.value)
    with kit["repo"]._sessions() as db:
        reservation = db.scalars(select(InferenceBudgetReservationRow)).one()
        assert reservation.status == "UNCERTAIN"
        assert reservation.held_microusd > 0
        assert reservation.charged_microusd == 0


def test_unmetered_gateway_rejects_nonexistent_task_when_budget_is_enabled(kit):
    _, _, prompt = fixture()
    ledger = InferenceBudgetLedger(kit["repo"], limits())
    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id="route_test_only", model_revision="mock_v1",
        ), OfflineMockInferenceTransport(), clock=lambda: NOW,
        budget_ledger=ledger,
    )
    with pytest.raises(InferenceDenied):
        gateway.infer(prompt)
    with kit["repo"]._sessions() as db:
        assert db.scalars(select(InferenceBudgetReservationRow)).all() == []


def test_public_api_exposes_no_spending_or_billing_reconciliation_route(kit):
    client, signer, employee, _, _, _, _ = setup(kit)
    task_id = new_task(client, signer, employee)
    assert client.get("/v1/billing").status_code == 404
    assert client.post("/v1/inference/budget/reconcile",
                       headers=auth(signer, employee), json={}).status_code == 404
    assert kit["repo"].replay_task(task_id).external_effects == 0
