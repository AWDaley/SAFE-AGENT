"""Billing reconciliation releases only UNKNOWN admission slots, never live dispatch."""
from datetime import timedelta

import pytest
from sqlalchemy import update

from safe_agent.storage.inference_admission import (
    InferenceAdmissionDenied,
    TrustedInferenceAdmission,
)
from safe_agent.storage.inference_budget import InferenceBudgetLedger
from safe_agent.storage.inference_dispatch import TrustedInferenceDispatchStore
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    InferenceInvocationRow,
)
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7.test_inference_budget import BILLING_KEY, sign_evidence


@pytest.fixture
def repo(tmp_path):
    value = LedgerRepository.testing_sqlite(
        f"sqlite+pysqlite:///{tmp_path / 'accounting_reconciliation.sqlite'}"
    )
    yield value
    value.close()


def started(repo, pol):
    ticks = [NOW]
    admission = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0])
    args = assigned(repo)
    receipt = admission.admit(*args)
    store = TrustedInferenceDispatchStore(admission)
    claim = store.claim(*args)
    assert claim is not None
    request, _ = store.start(*args, claim)
    return args, ticks, admission, store, request, receipt


def test_unknown_with_unreconciled_hold_still_blocks_outstanding_capacity(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    args, ticks, _, store, _, _ = started(repo, pol)
    ticks[0] += timedelta(minutes=2)
    assert store.recover(*args).state == "UNKNOWN"
    other = assigned(repo, org=args[0].organization_id, now=ticks[0])
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0]).admit(*other)


def test_attested_unknown_releases_outstanding_slot_but_keeps_real_charge(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    args, ticks, _, store, request, first = started(repo, pol)
    ticks[0] += timedelta(minutes=2)
    assert store.recover(*args).state == "UNKNOWN"

    other = assigned(repo, org=args[0].organization_id, now=ticks[0])
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0]).admit(*other)

    ledger = InferenceBudgetLedger(repo, pol.budget)
    settled = ledger.reconcile_attested(
        sign_evidence(request, amount=80),
        verifier_secret=BILLING_KEY,
        now=ticks[0],
    )
    assert settled.status == "RECONCILED"
    assert settled.held_microusd == 0
    assert settled.charged_microusd == 80
    assert settled.request_id == first.binding.request_id

    second = TrustedInferenceAdmission(
        repo, policy=pol, clock=lambda: ticks[0],
    ).admit(*other)
    assert second.disposition == "CREATED"
    assert second.binding.request_id != first.binding.request_id

    with repo._sessions() as db:
        old = db.get(InferenceBudgetReservationRow, first.binding.request_id)
        assert old is not None
        assert old.status == "RECONCILED"
        assert old.held_microusd == 0
        assert old.charged_microusd == 80


def test_financial_reconciliation_does_not_release_live_dispatch_slot(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    args, ticks, _, store, request, first = started(repo, pol)
    ticks[0] += timedelta(minutes=2)

    ledger = InferenceBudgetLedger(repo, pol.budget)
    settled = ledger.reconcile_attested(
        sign_evidence(request, amount=80),
        verifier_secret=BILLING_KEY,
        now=ticks[0],
    )
    assert settled.status == "RECONCILED"

    other = assigned(repo, org=args[0].organization_id, now=ticks[0])
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0]).admit(*other)

    with repo._sessions() as db:
        invocation = db.get(InferenceInvocationRow, first.binding.request_id)
        assert invocation is not None and invocation.state == "DISPATCH_STARTED"

    assert store.recover(*args).state == "UNKNOWN"
    second = TrustedInferenceAdmission(
        repo, policy=pol, clock=lambda: ticks[0],
    ).admit(*other)
    assert second.disposition == "CREATED"


def test_reconciliation_never_erases_rate_window(repo):
    pol = policy(
        global_requests=1, tenant_requests=1, task_requests=1,
        global_outstanding=1, tenant_outstanding=1, task_outstanding=1,
        window_seconds=300,
    )
    args, ticks, _, store, request, _ = started(repo, pol)
    ticks[0] += timedelta(minutes=2)
    assert store.recover(*args).state == "UNKNOWN"
    InferenceBudgetLedger(repo, pol.budget).reconcile_attested(
        sign_evidence(request, amount=0),
        verifier_secret=BILLING_KEY,
        now=ticks[0],
    )
    other = assigned(repo, org=args[0].organization_id, now=ticks[0])
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0]).admit(*other)


def test_mismatched_reconciliation_metadata_fails_closed_for_outstanding_count(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    args, ticks, _, store, request, first = started(repo, pol)
    ticks[0] += timedelta(minutes=2)
    assert store.recover(*args).state == "UNKNOWN"
    InferenceBudgetLedger(repo, pol.budget).reconcile_attested(
        sign_evidence(request, amount=80),
        verifier_secret=BILLING_KEY,
        now=ticks[0],
    )

    # SQLite is a trusted local-development database; this deliberately injects
    # corruption to ensure admission does not trust status=RECONCILED by itself.
    with repo.engine.begin() as db:
        db.execute(
            update(InferenceBudgetReservationRow)
            .where(InferenceBudgetReservationRow.request_id == first.binding.request_id)
            .values(evidence_hash="sha256:" + "0" * 64)
        )

    other = assigned(repo, org=args[0].organization_id, now=ticks[0])
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: ticks[0]).admit(*other)
