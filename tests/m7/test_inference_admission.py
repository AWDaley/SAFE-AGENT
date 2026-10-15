"""Atomic admission, idempotent identity and conservative restart quotas."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, update

from safe_agent.canonical import canonical_json
from safe_agent.contracts.inference_admission import (
    InferenceAdmissionPolicy,
    InferenceThroughputLimits,
)
from safe_agent.storage.inference_admission import (
    InferenceAdmissionDenied,
    TrustedInferenceAdmission,
)
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from safe_agent.storage.models import (
    InferenceAdmissionAuthorityRow,
    InferenceAdmissionRow,
    InferenceBudgetAccountRow,
    InferenceBudgetEventRow,
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
    InferenceInvocationRow,
    PlanningWorkspaceRow,
    TaskRow,
)
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy


@pytest.fixture
def repo(tmp_path):
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'admission.sqlite'}")
    # Enforce actual composite foreign keys in every SQLite test connection.
    @event.listens_for(repo.engine, "connect")
    def foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    with repo.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    yield repo
    repo.close()


def sizes(repo):
    with repo._sessions() as db:
        return tuple(db.scalar(select(func.count()).select_from(table)) for table in (
            InferenceAdmissionAuthorityRow, InferenceAdmissionRow, InferenceInvocationRow,
            InferenceInvocationEventRow, InferenceBudgetReservationRow, InferenceBudgetEventRow,
        ))


def test_atomic_admission_and_response_loss_replay(repo):
    args = assigned(repo)
    clock = [NOW]
    admission = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: clock[0])
    first = admission.admit(*args)
    assert first.disposition == "CREATED"
    assert first.reservation.held_microusd == 205
    assert first.invocation.state == "RESERVED"
    assert first.dispatch_authorized is False
    assert first.provider_effect_performed is False
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)
    clock[0] += timedelta(seconds=3)
    second = admission.admit(*args)
    assert second.disposition == "EXISTING"
    assert first.binding == second.binding
    assert first.reservation == second.reservation
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)
    with repo._sessions() as db:
        holds = db.scalars(select(InferenceBudgetAccountRow.held_microusd)).all()
        assert holds == [205, 205, 205]


def test_restart_and_expired_same_attempt_never_extend_deadline_or_refund(repo):
    args = assigned(repo)
    first = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    # Reopen the file with another repository/connection pool, not just a new dict.
    from sqlalchemy import create_engine
    reopened = LedgerRepository(create_engine(repo.engine.url))
    try:
        later = NOW + timedelta(minutes=6)
        service = TrustedInferenceAdmission(reopened, policy=policy(), clock=lambda: later)
        second = service.admit(*args)
        assert second.binding == first.binding
        assert second.reservation.held_microusd == 205
        assert second.disposition == "EXISTING" and not second.dispatch_authorized
        assert sizes(repo) == (1, 1, 1, 1, 1, 1)
    finally:
        reopened.close()


@pytest.mark.parametrize("table", [InferenceBudgetEventRow, InferenceInvocationRow,
                                   InferenceInvocationEventRow, InferenceAdmissionRow])
def test_failure_at_each_write_rolls_back_everything(repo, table):
    args = assigned(repo)
    def broken(*_):
        raise RuntimeError("PRIVATE_DATABASE_SECRET")
    event.listen(table, "before_insert", broken)
    try:
        with pytest.raises(InferenceAdmissionDenied, match="^INFERENCE_ADMISSION_DENIED$"):
            TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    finally:
        event.remove(table, "before_insert", broken)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)
    with repo._sessions() as db:
        assert db.scalar(select(func.count()).select_from(InferenceBudgetAccountRow)) == 0
    # A failed transaction did not consume this attempt's admission identity.
    assert TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args).disposition == "CREATED"


def test_failure_after_all_flushes_before_commit_is_atomic(repo):
    args = assigned(repo)
    def broken(session):
        if session.scalar(select(func.count()).select_from(InferenceAdmissionRow)):
            raise RuntimeError("commit rejected")
    event.listen(repo._sessions, "before_commit", broken)
    try:
        with pytest.raises(InferenceAdmissionDenied):
            TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    finally:
        event.remove(repo._sessions, "before_commit", broken)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


def test_process_crash_exception_rolls_back_without_being_swallowed(repo):
    args = assigned(repo)
    class Crash(BaseException):
        pass
    def crash(*_):
        raise Crash()
    event.listen(InferenceAdmissionRow, "before_insert", crash)
    try:
        with pytest.raises(Crash):
            TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    finally:
        event.remove(InferenceAdmissionRow, "before_insert", crash)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("change", [
    {"actor_id": "foreign_actor"}, {"organization_id": "foreign_org"},
    {"scopes": ("task:read",)}, {"authenticated_until": NOW},
])
def test_principal_denial_writes_nothing(repo, change):
    actor, lease, prompt = assigned(repo)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(
            actor.model_copy(update=change), lease, prompt,
        )
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("change", [
    {"workspace_version": 2}, {"task_state_version": 1}, {"attempt_number": 2},
    {"attempt_id": "plan_nonexistent"}, {"mode": "deterministic"},
    {"expires_at": NOW + timedelta(seconds=120)}, {"task_state_version": False},
    {"manifest_hash": "sha256:" + "0" * 64},
])
def test_forged_or_stale_lease_is_not_authority(repo, change):
    actor, lease, prompt = assigned(repo)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(
            actor, lease.model_copy(update=change), prompt,
        )
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("part,field,value", [
    ("task", "organization_id", "foreign_org"), ("task", "calendar_ref", "foreign_calendar"),
    ("task", "customer_ref", "foreign_customer"), ("task", "template_id", "foreign_template"),
    ("task", "duration_minutes", True), ("task", "duration_minutes", 90),
    ("availability", "revision", "different_projection"),
])
def test_altered_repeat_is_denied_without_replacing_binding(repo, part, field, value):
    actor, lease, prompt = assigned(repo)
    service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
    first = service.admit(actor, lease, prompt)
    obj = json.loads(prompt)
    obj[part][field] = value
    with pytest.raises(InferenceAdmissionDenied):
        service.admit(actor, lease, canonical_json(obj).decode())
    assert service.admit(actor, lease, prompt).binding == first.binding
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("bad", ['{}', 'not json', '{"x":1,"x":2}', '{"x":NaN}',
                                '{"provider_url":"https://invalid.example"}'])
def test_malformed_input_never_admits(repo, bad):
    actor, lease, _ = assigned(repo)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(actor, lease, bad)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


def test_stale_workspace_and_task_metadata_denied(repo):
    actor, lease, prompt = assigned(repo)
    with repo.engine.begin() as db:
        db.execute(update(PlanningWorkspaceRow).where(
            PlanningWorkspaceRow.task_id == lease.task_id,
        ).values(workspace_version=99))
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(actor, lease, prompt)
    with repo.engine.begin() as db:
        db.execute(update(PlanningWorkspaceRow).where(
            PlanningWorkspaceRow.task_id == lease.task_id,
        ).values(workspace_version=lease.workspace_version))
        db.execute(update(TaskRow).where(TaskRow.task_id == lease.task_id).values(state_version=9))
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(actor, lease, prompt)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


def test_expired_new_attempt_and_regressed_clock_fail_closed(repo):
    args = assigned(repo)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW + timedelta(seconds=90)).admit(*args)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)
    service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW + timedelta(seconds=2))
    service.admit(*args)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)


def test_concurrent_identical_attempt_has_one_reservation(repo):
    args = assigned(repo)
    barrier = Barrier(2)
    def submit(_):
        service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
        barrier.wait(timeout=3)
        return service.admit(*args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit, (1, 2)))
    assert sorted(r.disposition for r in replies) == ["CREATED", "EXISTING"]
    assert replies[0].binding == replies[1].binding
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)


def test_persistent_global_limit_across_tenants_and_restarts(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    a, b = assigned(repo, org="org_a"), assigned(repo, org="org_b")
    TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW).admit(*a)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW).admit(*b)
    # A zero price is still an admission: it cannot bypass throughput controls.
    changed = pol.model_copy(update={"route": pol.route.model_copy(update={
        "output_microusd_per_million_tokens": 0,
    })})
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=changed, clock=lambda: NOW).admit(*b)
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)


def test_rate_window_survives_restart_without_refunding_cost(repo):
    pol = policy(global_requests=1, tenant_requests=1, task_requests=1,
                 global_outstanding=5, tenant_outstanding=5, task_outstanding=1)
    a = assigned(repo)
    first = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW).admit(*a)
    b = assigned(repo, now=NOW + timedelta(seconds=30))
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW + timedelta(seconds=30)).admit(*b)
    later = NOW + timedelta(seconds=60)
    TrustedInferenceAdmission(repo, policy=pol, clock=lambda: later).admit(*b)
    ledger = InferenceBudgetLedger(repo, pol.budget)
    assert ledger.receipt(a[0], first.binding.request_id, now=later).held_microusd == 205
    assert sizes(repo) == (1, 2, 2, 2, 2, 2)


def test_configuration_cannot_be_reset_after_restart(repo):
    args = assigned(repo)
    pol = policy()
    TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW).admit(*args)
    variants = [
        pol.model_copy(update={"throughput": pol.throughput.model_copy(update={"global_requests": 61})}),
        pol.model_copy(update={"budget": pol.budget.model_copy(update={"global_microusd": 20000})}),
        pol.model_copy(update={"route": pol.route.model_copy(update={"model_revision": "new_model"})}),
    ]
    for altered in variants:
        with pytest.raises(InferenceAdmissionDenied):
            TrustedInferenceAdmission(repo, policy=altered, clock=lambda: NOW).admit(*args)
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)


def test_expired_hold_and_unverified_billing_never_free_admission(repo):
    pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    a = assigned(repo)
    first = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW).admit(*a)
    later = NOW + timedelta(minutes=10)
    ledger = InferenceBudgetLedger(repo, pol.budget)
    assert ledger.mark_expired_uncertain(now=later) == 1
    assert ledger.receipt(a[0], first.binding.request_id, now=later).held_microusd == 205
    b = assigned(repo, now=later)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=pol, clock=lambda: later).admit(*b)
    with pytest.raises(InferenceBudgetDenied):
        ledger.close_synthetic(first.binding.request_id, exact_offline_mock=True, now=later)
    assert sizes(repo) == (1, 1, 1, 1, 1, 2)


@pytest.mark.parametrize("column,value", [
    ("intent_hash", "sha256:" + "0" * 64),
    ("policy_hash", "sha256:" + "1" * 64),
])
def test_sqlite_admission_metadata_tamper_is_detected(repo, column, value):
    args = assigned(repo)
    service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
    service.admit(*args)
    # Isolated SQLite deliberately has no PostgreSQL immutability triggers.
    # Disable FK only for this corruption injection, then restore it.
    with repo.engine.connect() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=OFF")
        db.execute(update(InferenceAdmissionRow).values({column: value}))
        db.commit()
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(InferenceAdmissionDenied):
        service.admit(*args)


def test_no_raw_prompt_or_model_interface_and_policy_is_closed(repo):
    args = assigned(repo)
    service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
    receipt = service.admit(*args)
    for forbidden in ("generate", "infer", "dispatch", "refund", "reset"):
        assert not hasattr(service, forbidden)
    assert "free_slots" not in receipt.model_dump_json()
    assert "customer_" not in receipt.model_dump_json()
    assert "global_microusd" not in receipt.model_dump_json()
    with pytest.raises(ValidationError):
        InferenceThroughputLimits(global_requests=True)
    with pytest.raises(ValidationError):
        InferenceAdmissionPolicy(**{**policy().model_dump(), "provider_url": "https://invalid.example"})
    with pytest.raises(TypeError):
        TrustedInferenceAdmission(repo, policy=policy(), budget_ledger=None)


def test_deadline_expiring_during_budget_contention_rolls_back(repo):
    args = assigned(repo)
    ticks = iter((NOW, NOW, NOW, NOW + timedelta(seconds=11)))
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: next(ticks)).admit(*args)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


def test_budget_failure_after_first_account_update_is_atomic(repo, monkeypatch):
    import safe_agent.storage.inference_budget as budgeting
    args = assigned(repo)
    original = budgeting._change_account
    count = [0]
    def break_second(*a, **kw):
        count[0] += 1
        if count[0] == 2:
            raise RuntimeError("synthetic failure after global balance update")
        return original(*a, **kw)
    monkeypatch.setattr(budgeting, "_change_account", break_second)
    with pytest.raises(InferenceAdmissionDenied):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    assert count[0] == 2
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)
    with repo._sessions() as db:
        assert db.scalar(select(func.count()).select_from(InferenceBudgetAccountRow)) == 0


def test_zero_cost_admission_still_consumes_persistent_quota(repo):
    pol = policy(global_requests=1, tenant_requests=1, task_requests=1)
    pol = pol.model_copy(update={"route": pol.route.model_copy(update={
        "output_microusd_per_million_tokens": 0,
    })})
    a, b = assigned(repo), assigned(repo)
    service = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW)
    first = service.admit(*a)
    assert first.reservation.held_microusd == 0
    assert first.reservation.status == "IN_FLIGHT"
    with pytest.raises(InferenceAdmissionDenied):
        service.admit(*b)
    assert sizes(repo) == (1, 1, 1, 1, 1, 1)


def test_database_failure_has_no_success_or_memory_fallback(repo, monkeypatch):
    args = assigned(repo)
    def unavailable(*a, **kw):
        raise RuntimeError("PRIVATE_DB_CREDENTIAL")
    monkeypatch.setattr(repo, "write_task", unavailable)
    with pytest.raises(InferenceAdmissionDenied, match="^INFERENCE_ADMISSION_DENIED$"):
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW).admit(*args)
    assert sizes(repo) == (0, 0, 0, 0, 0, 0)


def test_admission_uses_actual_m6_registered_task_and_planning_lease(kit):
    from tests.m6.test_persistent_planning import setup
    from tests.m6.test_synthetic_workflow import new_task
    from tests.m7.test_inference_budget import make_request
    client, signer, actor, _, _, _, store = setup(kit)
    task_id = new_task(client, signer, actor)
    lease = store.begin(actor, task_id, expected_task_version=0,
                        expected_workspace_version=0, mode="text_only", now=NOW)
    _, prompt = make_request(kit, task_id)
    receipt = TrustedInferenceAdmission(kit["repo"], policy=policy(), clock=lambda: NOW).admit(
        actor, lease, prompt,
    )
    assert receipt.binding.attempt_id == lease.attempt_id
    assert receipt.dispatch_authorized is False
    assert kit["repo"].read_task(task_id).external_effects == 0
    assert len(kit["repo"].read_events(task_id)) == 1
