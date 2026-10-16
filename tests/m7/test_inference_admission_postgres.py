"""Real PostgreSQL transactions: mandatory in explicit disposable CI job."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from safe_agent.storage.inference_admission import (
    InferenceAdmissionDenied,
    TrustedInferenceAdmission,
)
from safe_agent.storage.models import (
    InferenceAdmissionAuthorityRow,
    InferenceAdmissionRow,
    InferenceBudgetAccountRow,
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
)
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7 import test_inference_accounting_reconciliation as accounting_cases
from tests.m7.test_inference_admission import sizes

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_ADMISSION") != "1",
    reason="requires explicit disposable PostgreSQL admission acceptance job",
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_repo():
    url = os.environ.get("SAFE_AGENT_DATABASE_URL", "")
    assert url.startswith("postgresql+psycopg://"), "real PostgreSQL is mandatory"
    # Every test owns a fresh schema; no mutable global profile resets or fixture
    # monkeypatches of transaction semantics. Other test jobs' data are untouched.
    schema = "admission_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(scoped_url)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
            env={**os.environ, "SAFE_AGENT_DATABASE_URL": scoped_url.render_as_string(hide_password=False)},
            capture_output=True, text=True, timeout=35, check=False,
        )
        assert result.returncode == 0, "isolated PostgreSQL migration failed"
        yield LedgerRepository(engine)
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def race(repo, calls, pol):
    barrier = Barrier(len(calls))
    def invoke(args):
        # Separate coordinator and connection per competing caller.
        service = TrustedInferenceAdmission(repo, policy=pol, clock=lambda: NOW)
        barrier.wait(timeout=5)
        try:
            return service.admit(*args)
        except InferenceAdmissionDenied:
            return None
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(invoke, calls))


def test_postgres_duplicate_attempt_commits_exactly_one_reservation(pg_repo):
    args = assigned(pg_repo)
    answers = race(pg_repo, [args, args], policy())
    assert all(answer is not None for answer in answers)
    assert sorted(answer.disposition for answer in answers) == ["CREATED", "EXISTING"]
    assert answers[0].binding == answers[1].binding
    assert sizes(pg_repo) == (1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("scope", ["global", "tenant", "task"])
def test_postgres_simultaneous_admission_respects_scope_caps(pg_repo, scope):
    args = [assigned(pg_repo, org="org_a"), assigned(pg_repo, org="org_a")]
    pol = policy()
    if scope == "global":
        args[1] = assigned(pg_repo, org="org_b")
        pol = policy(global_outstanding=1, tenant_outstanding=1, task_outstanding=1)
    elif scope == "tenant":
        pol = policy(global_outstanding=5, tenant_outstanding=1, task_outstanding=1)
    else:
        # For a later attempt on the SAME task, an earlier reservation still
        # consumes capacity even if the planning lease was explicitly failed.
        from safe_agent.storage.planning import PlanningWorkspaceStore
        a = args[0]
        TrustedInferenceAdmission(pg_repo, policy=pol, clock=lambda: NOW).admit(*a)
        store = PlanningWorkspaceStore(pg_repo)
        store.fail(a[1], result_kind="RETRYABLE_FAILURE", reason_code="MODEL_RESPONSE_REJECTED", now=NOW)
        lease = store.begin(a[0], a[1].task_id, expected_task_version=0,
                            expected_workspace_version=2, mode="text_only", now=NOW)
        with pytest.raises(InferenceAdmissionDenied):
            TrustedInferenceAdmission(pg_repo, policy=pol, clock=lambda: NOW).admit(a[0], lease, a[2])
        return
    answers = race(pg_repo, args, pol)
    assert sum(a is not None for a in answers) == 1
    assert sizes(pg_repo) == (1, 1, 1, 1, 1, 1)
    with pg_repo._sessions() as db:
        assert db.scalars(select(InferenceBudgetAccountRow.held_microusd)).all() == [205, 205, 205]


def test_postgres_late_failure_rolls_back_budget_and_admission(pg_repo):
    args = assigned(pg_repo)
    def fail(*_):
        raise RuntimeError("synthetic failure after cost reservation")
    event.listen(InferenceAdmissionRow, "before_insert", fail)
    try:
        with pytest.raises(InferenceAdmissionDenied):
            TrustedInferenceAdmission(pg_repo, policy=policy(), clock=lambda: NOW).admit(*args)
    finally:
        event.remove(InferenceAdmissionRow, "before_insert", fail)
    assert sizes(pg_repo) == (0, 0, 0, 0, 0, 0)
    with pg_repo._sessions() as db:
        assert db.scalar(select(func.count()).select_from(InferenceBudgetAccountRow)) == 0


def test_postgres_metadata_and_policy_cannot_be_deleted_or_rewritten(pg_repo):
    args = assigned(pg_repo)
    TrustedInferenceAdmission(pg_repo, policy=policy(), clock=lambda: NOW).admit(*args)
    edits = [
        update(InferenceAdmissionRow).values(intent_hash="sha256:" + "0" * 64),
        delete(InferenceAdmissionRow),
        delete(InferenceInvocationEventRow),
        update(InferenceAdmissionAuthorityRow).values(policy_hash="sha256:" + "1" * 64),
        delete(InferenceAdmissionAuthorityRow),
        update(InferenceAdmissionAuthorityRow).values(last_seen_at=NOW - timedelta(seconds=1)),
    ]
    for edit in edits:
        with pytest.raises(DBAPIError), pg_repo.engine.begin() as db:
            db.execute(edit)
    assert sizes(pg_repo) == (1, 1, 1, 1, 1, 1)


def test_postgres_reopened_repository_preserves_identity_holds_and_rate(pg_repo):
    args = assigned(pg_repo)
    pol = policy(global_requests=1, tenant_requests=1, task_requests=1)
    first = TrustedInferenceAdmission(pg_repo, policy=pol, clock=lambda: NOW).admit(*args)
    other = assigned(pg_repo, org="org_other")
    reopened = LedgerRepository(create_engine(pg_repo.engine.url))
    try:
        later = NOW + timedelta(seconds=2)
        service = TrustedInferenceAdmission(reopened, policy=pol, clock=lambda: later)
        assert service.admit(*args).binding == first.binding
        with pytest.raises(InferenceAdmissionDenied):
            service.admit(*other)
        with reopened._sessions() as db:
            assert db.scalars(select(InferenceBudgetReservationRow.held_microusd)).all() == [205]
    finally:
        reopened.close()


def test_postgres_attested_unknown_releases_outstanding_capacity(pg_repo):
    accounting_cases.test_attested_unknown_releases_outstanding_slot_but_keeps_real_charge(
        pg_repo,
    )


def test_postgres_reconciled_live_dispatch_still_consumes_outstanding_capacity(pg_repo):
    accounting_cases.test_financial_reconciliation_does_not_release_live_dispatch_slot(
        pg_repo,
    )
