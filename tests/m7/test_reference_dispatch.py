"""Reference revocation and durable dispatch ordering; no live provider calls.

Calendar cases use normal coordinated planning, separate manager approval, the
reviewed TAL runtime, real SQL transactions and the durable synthetic provider.
Hooks only coordinate competing actions or simulate process interruption.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text, update

from safe_agent.adapters.fake_calendar import FakeCalendarReader, FakeCalendarWriter
from safe_agent.broker import BrokerScope, ReferenceBroker, ReferenceDenied, ResourceKind
from safe_agent.contracts.intake import TaskIntakeRequest
from safe_agent.contracts.planning import PlanningCommand
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.orchestration.coordinated_queue import queue_coordinated_task
from safe_agent.orchestration.office_tasks import ApprovalInput, TrustedOfficeTasks
from safe_agent.storage.models import OutboxRow, ReferenceRow
from safe_agent.storage.repository import LedgerRepository, StateConflict, TaskTransaction
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.tal.trusted_dispatch import TrustedTALOfficeDispatchCheck
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import COMMAND, setup
from tests.m6.test_task_intake import payload, provisioner
from tests.m7.test_coordinated_planning import build
from tests.m7.test_inference_dispatch_schema import migrate, sqlite_engine

BROKER_KEY = b"m2_test_private_broker_key_never_prod_000001"


def staged(kit, monkeypatch, *, internal_task=False):
    """Prepare/approve/queue through supported services without an HTTP thread."""
    if internal_task:
        _, _, actor, calendar, internal, legacy, _ = setup(kit)
        planning = None
        workflow = legacy._workflow
    else:
        _, _, actor, calendar, internal, planning, _, _ = build(kit, monkeypatch)
        workflow = planning._workflow
    request = TaskIntakeRequest.model_validate_json(json.dumps(payload(
        "opt_backoffice" if internal_task else "opt_followup",
    )))
    task_id = provisioner(kit).create(actor, request, now=NOW).task_id
    if internal_task:
        prepared = workflow.prepare(actor, task_id, expected_state_version=0, now=NOW)
        workflow.queue(actor, task_id, expected_state_version=prepared.state_version, now=NOW)
    else:
        planning.plan(actor, task_id, PlanningCommand.model_validate(COMMAND), now=NOW)
        snap = kit["repo"].read_task(task_id)
        approved = kit["coordinator"].approve(kit["manager"], task_id, ApprovalInput(
            proposal_hash=snap.proposal_hash, expected_state_version=snap.state_version,
            approved=True, expires_at=NOW + timedelta(minutes=20),
        ), now=NOW)
        queue_coordinated_task(planning, actor, task_id,
                               expected_state_version=approved.state_version, now=NOW)
    return SimpleNamespace(repo=kit["repo"], task_id=task_id, actor=actor,
                           calendar=calendar, internal=internal)


def worker(flow, *, worker_id=None):
    # A different broker from both the planner and the revoking process is vital
    # to exercising stale cached authority across independent workers.
    return SyntheticRecoveryWorker(
        flow.repo, ReferenceBroker(secret=BROKER_KEY), flow.calendar,
        task_adapter=flow.internal, worker_id=worker_id,
        tal_dispatch=TrustedTALOfficeDispatchCheck(),
    )


def revoked_records(flow):
    broker = ReferenceBroker(secret=BROKER_KEY)
    original = flow.repo.load_protected_references(flow.task_id, "org_demo")
    broker.trusted_import(original)
    for entry in original:
        broker.revoke(entry["handle"])
    return broker.trusted_export(task_id=flow.task_id)


def persist_revocation(flow, records=None):
    """Exercise the supported broker + repository path, not raw SQL mutation."""
    flow.repo.persist_protected_references(
        flow.task_id, "org_demo", flow.actor.actor_id,
        records if records is not None else revoked_records(flow),
    )
    with flow.repo._sessions() as db:
        rows = db.scalars(select(ReferenceRow).where(ReferenceRow.task_id == flow.task_id)).all()
        assert rows and all(row.revoked and row.binding_json["record"]["revoked"] for row in rows)


def assert_no_effect(flow, receipt):
    assert receipt is not None and receipt.outcome == "OUTCOME_UNKNOWN"
    assert flow.calendar.effect_count == flow.internal.effect_count == 0
    assert flow.repo.read_outbox(flow.task_id)["status"] == "NEEDS_REVIEW"
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.NEEDS_REVIEW


@pytest.mark.parametrize("when", ["before_worker", "after_readback", "after_source_check"])
def test_revocation_before_final_admission_prevents_new_effect(kit, monkeypatch, when):
    flow = staged(kit, monkeypatch)
    reached = []
    if when == "before_worker":
        persist_revocation(flow)
    elif when == "after_readback":
        original = flow.calendar.find_effect
        def find(**kwargs):
            found = original(**kwargs)
            assert found is None
            persist_revocation(flow)
            reached.append(True)
            return found
        monkeypatch.setattr(flow.calendar, "find_effect", find)
    else:
        original = FakeCalendarReader.snapshot
        def snapshot(self, **kwargs):
            observed = original(self, **kwargs)
            persist_revocation(flow)
            reached.append(True)
            return observed
        monkeypatch.setattr(FakeCalendarReader, "snapshot", snapshot)
    assert_no_effect(flow, worker(flow).process_one(task_id=flow.task_id, now=NOW))
    assert when == "before_worker" or reached == [True]


@pytest.mark.parametrize("mutation", ["row_flag", "envelope_flag", "bad_mac", "missing"])
def test_current_reference_rows_are_required_even_after_initial_readback(kit, monkeypatch, mutation):
    flow = staged(kit, monkeypatch)
    original = flow.calendar.find_effect
    reached = []
    def find(**kwargs):
        assert original(**kwargs) is None
        with flow.repo._sessions.begin() as db:
            row = db.scalars(select(ReferenceRow).where(
                ReferenceRow.task_id == flow.task_id,
            ).order_by(ReferenceRow.handle)).first()
            assert row is not None
            if mutation == "missing":
                db.execute(delete(ReferenceRow).where(ReferenceRow.handle == row.handle))
            elif mutation == "row_flag":
                db.execute(update(ReferenceRow).where(ReferenceRow.handle == row.handle)
                           .values(revoked=True))
            else:
                envelope = deepcopy(row.binding_json)
                if mutation == "envelope_flag":
                    envelope["record"]["revoked"] = True
                else:
                    envelope["mac"] = "0" * 64
                db.execute(update(ReferenceRow).where(ReferenceRow.handle == row.handle)
                           .values(binding_json=envelope))
        reached.append(True)
        return None
    monkeypatch.setattr(flow.calendar, "find_effect", find)
    assert_no_effect(flow, worker(flow).process_one(task_id=flow.task_id, now=NOW))
    assert reached == [True]


def test_committed_dispatch_admission_precedes_later_revocation(kit, monkeypatch):
    flow = staged(kit, monkeypatch)
    original = FakeCalendarWriter.create
    admitted = []
    def create(self, **kwargs):
        # An independent transaction observes the fence before any provider write.
        assert flow.repo.read_outbox(flow.task_id)["status"] == "DISPATCHING"
        assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.EXECUTING
        assert flow.calendar.effect_count == 0
        persist_revocation(flow)
        admitted.append(True)
        return original(self, **kwargs)
    monkeypatch.setattr(FakeCalendarWriter, "create", create)
    result = worker(flow).process_one(task_id=flow.task_id, now=NOW)
    assert result is not None and result.outcome == "COMMITTED"
    assert admitted == [True] and flow.calendar.effect_count == 1
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.COMPLETED


@pytest.mark.parametrize("revoked", [False, True])
def test_crash_after_admission_requires_fresh_authority_when_effect_absent(kit, monkeypatch, revoked):
    flow = staged(kit, monkeypatch)
    with monkeypatch.context() as crash_patch:
        def crash(self, **kwargs):
            assert flow.repo.read_outbox(flow.task_id)["status"] == "DISPATCHING"
            raise InjectedCrash("process exit after SQL admission, before provider send")
        crash_patch.setattr(FakeCalendarWriter, "create", crash)
        with pytest.raises(InjectedCrash):
            worker(flow).process_one(task_id=flow.task_id, now=NOW)
    assert flow.calendar.effect_count == 0
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.EXECUTING
    assert worker(flow).process_one(task_id=flow.task_id, now=NOW) is None
    if revoked:
        persist_revocation(flow)
    result = worker(flow).process_one(task_id=flow.task_id, now=NOW + timedelta(seconds=31))
    if revoked:
        assert_no_effect(flow, result)
    else:
        assert result is not None and result.outcome == "COMMITTED"
        assert flow.calendar.effect_count == 1
        assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.COMPLETED


@pytest.mark.parametrize("internal_task", [False, True])
def test_existing_effect_is_verified_after_revocation_and_restart(kit, monkeypatch, internal_task):
    flow = staged(kit, monkeypatch, internal_task=internal_task)
    with pytest.raises(InjectedCrash):
        worker(flow).process_one(task_id=flow.task_id, now=NOW, fault=FaultPoint.AFTER_PROVIDER_COMMIT)
    provider = flow.internal if internal_task else flow.calendar
    assert provider.effect_count == 1
    persist_revocation(flow)
    after = worker(flow)
    def forbidden(*args, **kwargs):
        raise AssertionError("existing-effect readback must not grant new dispatch authority")
    monkeypatch.setattr(after, "_allow_new_dispatch", forbidden)
    # Original projection and approval are now expired; this is readback only.
    result = after.process_one(task_id=flow.task_id, now=NOW + timedelta(hours=1))
    assert result is not None and result.outcome == "COMMITTED"
    assert provider.effect_count == 1
    assert after.process_one(task_id=flow.task_id, now=NOW + timedelta(hours=1)) is None
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.COMPLETED


def test_internal_task_checks_current_references_before_new_write(kit, monkeypatch):
    flow = staged(kit, monkeypatch, internal_task=True)
    original = flow.internal.find_effect
    def find(**kwargs):
        assert original(**kwargs) is None
        persist_revocation(flow)
        return None
    monkeypatch.setattr(flow.internal, "find_effect", find)
    assert_no_effect(flow, worker(flow).process_one(task_id=flow.task_id, now=NOW))


def test_expired_claim_cannot_use_reacquired_lease_with_same_worker_id(kit, monkeypatch):
    flow = staged(kit, monkeypatch)
    first = flow.repo.claim_action(task_id=flow.task_id, worker_id="same_worker", now=NOW)
    later = NOW + timedelta(seconds=31)
    second = flow.repo.claim_action(task_id=flow.task_id, worker_id="same_worker", now=later)
    assert first is not None and second is not None and second.attempts == first.attempts + 1
    stale = worker(flow, worker_id="same_worker")
    with pytest.raises(StateConflict, match="exact outbox claim"):
        stale._apply_claim(first, now=later, fault=None)
    with pytest.raises(StateConflict, match="exact outbox claim"):
        flow.repo.release_action_unknown(first, now=later, reason="STALE_WORKER")
    assert flow.calendar.effect_count == 0
    result = stale._apply_claim(second, now=later, fault=None)
    assert result.outcome == "COMMITTED" and flow.calendar.effect_count == 1


def test_superseded_process_cannot_finalize_an_already_admitted_effect(kit, monkeypatch):
    flow = staged(kit, monkeypatch)
    original = FakeCalendarWriter.create
    successor = []
    later = NOW + timedelta(seconds=31)
    def create(self, **kwargs):
        assert flow.repo.read_outbox(flow.task_id)["status"] == "DISPATCHING"
        persist_revocation(flow)
        claim = flow.repo.claim_action(task_id=flow.task_id, worker_id="same_worker", now=later)
        assert claim is not None and claim.preceding_status == "DISPATCHING" and claim.attempts == 2
        successor.append(claim)
        # The earlier admitted provider request may finish after lease expiry.
        return original(self, **kwargs)
    monkeypatch.setattr(FakeCalendarWriter, "create", create)
    with pytest.raises(StateConflict, match="exact outbox claim"):
        worker(flow, worker_id="same_worker").process_one(task_id=flow.task_id, now=NOW)
    assert flow.calendar.effect_count == 1
    assert flow.repo.read_outbox(flow.task_id)["status"] == "LEASED"
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.EXECUTING
    # Neither verification nor escalation by the old process changed the newer
    # lease. Its owner independently reads back the effect despite revocation.
    result = worker(flow, worker_id="same_worker")._apply_claim(successor[0], now=later, fault=None)
    assert result.outcome == "COMMITTED" and flow.calendar.effect_count == 1
    assert flow.repo.replay_task(flow.task_id).phase is OfficePhase.COMPLETED


@pytest.mark.parametrize("outcome", ["verified", "review", "unknown"])
def test_result_transaction_keeps_claim_fenced_until_commit(kit, monkeypatch, outcome):
    flow = staged(kit, monkeypatch)
    if outcome == "review":
        persist_revocation(flow)
    held, release, competing_lock = Event(), Event(), Event()
    competing_name = []
    original = TaskTransaction.require_outbox_claim
    later = NOW + timedelta(seconds=31)

    def fence(self, claim, **kwargs):
        row = original(self, claim, **kwargs)
        if kwargs.get("statuses") == ("LEASED", "DISPATCHING") and not held.is_set():
            held.set()
            assert release.wait(timeout=8), "test did not release outcome transaction"
        return row

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if current_thread().name in competing_name and (
            statement.startswith("UPDATE safe_action_outbox")
            or ("safe_action_outbox" in statement and "SKIP LOCKED" in statement)
        ):
            competing_lock.set()

    def claim():
        competing_name.append(current_thread().name)
        return flow.repo.claim_action(task_id=flow.task_id, worker_id="same_worker", now=later)

    monkeypatch.setattr(TaskTransaction, "require_outbox_claim", fence)
    event.listen(flow.repo.engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                executing = pool.submit(
                    worker(flow, worker_id="same_worker").process_one,
                    task_id=flow.task_id, now=NOW,
                    fault=FaultPoint.RESPONSE_LOST if outcome == "unknown" else None,
                )
                assert held.wait(timeout=8)
                takeover = pool.submit(claim)
                assert competing_lock.wait(timeout=8)
                if flow.repo.engine.dialect.name == "postgresql":
                    assert takeover.result(timeout=2) is None  # FOR UPDATE SKIP LOCKED.
                else:
                    with pytest.raises(TimeoutError):
                        takeover.result(timeout=0.1)
            finally:
                release.set()
            result = executing.result(timeout=8)
            assert takeover.result(timeout=8) is None
    finally:
        release.set()
        event.remove(flow.repo.engine, "before_cursor_execute", observe)
    assert result is not None
    status = flow.repo.read_outbox(flow.task_id)
    assert status["attempts"] == 1
    assert status["status"] == {
        "verified": "VERIFIED", "review": "NEEDS_REVIEW", "unknown": "OUTCOME_UNKNOWN",
    }[outcome]
    if outcome == "unknown":
        after = worker(flow, worker_id="same_worker").process_one(task_id=flow.task_id, now=later)
        assert after is not None and after.outcome == "COMMITTED"
    assert flow.calendar.effect_count == (0 if outcome == "review" else 1)


@pytest.mark.parametrize("first", ["dispatch", "revocation"])
def test_dispatch_and_revocation_serialize_on_task_lock(kit, monkeypatch, first):
    """Hold the winner's actual SQL transaction while the loser requests its lock."""
    flow = staged(kit, monkeypatch)
    revoked = revoked_records(flow)
    held, release, competing_lock = Event(), Event(), Event()
    competing_name = []
    original = flow.calendar.find_effect

    def hold(session, *_):
        winner = any(
            (isinstance(row, OutboxRow) and row.status == "DISPATCHING"
             if first == "dispatch" else isinstance(row, ReferenceRow) and row.revoked)
            for row in session.dirty
        )
        if winner and not held.is_set():
            held.set()
            assert release.wait(timeout=8), "test did not release held SQL transaction"

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if current_thread().name not in competing_name:
            return
        if statement == "BEGIN IMMEDIATE" or ("safe_tasks" in statement and "FOR UPDATE" in statement):
            competing_lock.set()

    def revoke():
        persist_revocation(flow, revoked)

    def dispatch():
        return worker(flow).process_one(task_id=flow.task_id, now=NOW)

    def competing(operation):
        competing_name.append(current_thread().name)
        return operation()

    event.listen(flow.repo._sessions, "before_flush", hold)
    event.listen(flow.repo.engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                if first == "dispatch":
                    executing = pool.submit(dispatch)
                    assert held.wait(timeout=8)
                    revoking = pool.submit(competing, revoke)
                    blocked = revoking
                else:
                    # Initial readback must complete before revocation takes the
                    # task lock, so only the final worker admission is contested.
                    readback = Event()
                    def find(**kwargs):
                        found = original(**kwargs)
                        readback.set()
                        assert held.wait(timeout=8)
                        competing_name.append(current_thread().name)
                        return found
                    monkeypatch.setattr(flow.calendar, "find_effect", find)
                    executing = pool.submit(dispatch)
                    assert readback.wait(timeout=8)
                    revoking = pool.submit(revoke)
                    assert held.wait(timeout=8)
                    blocked = executing
                assert competing_lock.wait(timeout=8), "loser did not request its SQL lock"
                with pytest.raises(TimeoutError):
                    blocked.result(timeout=0.1)
            finally:
                release.set()
            result = executing.result(timeout=8)
            revoking.result(timeout=8)
    finally:
        release.set()
        event.remove(flow.repo._sessions, "before_flush", hold)
        event.remove(flow.repo.engine, "before_cursor_execute", observe)
    if first == "dispatch":
        assert result is not None and result.outcome == "COMMITTED"
        assert flow.calendar.effect_count == 1
    else:
        assert_no_effect(flow, result)


def test_readback_resolution_preserves_integrity_scope_and_revocation(kit, monkeypatch):
    flow = staged(kit, monkeypatch)
    persist_revocation(flow)
    records = flow.repo.load_protected_references(flow.task_id, "org_demo")
    entry = next(record for record in records if record["record"]["resource_kind"] == "calendar")
    broker = ReferenceBroker(secret=BROKER_KEY)
    broker.trusted_import(records)
    scope = BrokerScope(organization_id="org_demo", initiator_id=flow.actor.actor_id,
                        task_id=flow.task_id, operation="calendar.create_event", now=NOW)
    assert broker.trusted_resolve_readback(
        entry["handle"], protected_records=records, scope=scope, kind=ResourceKind.CALENDAR,
    ) == "cal_main_private"
    with pytest.raises(ReferenceDenied):
        broker.resolve(entry["handle"], scope=scope, kind=ResourceKind.CALENDAR)
    with pytest.raises(ReferenceDenied):
        broker.trusted_resolve_readback(entry["handle"], protected_records=records,
            scope=scope.model_copy(update={"task_id": "different_task"}), kind=ResourceKind.CALENDAR)
    changed = deepcopy(entry)
    changed["record"]["resource_id"] = "changed_private_resource"
    with pytest.raises(ReferenceDenied):
        broker.trusted_resolve_readback(entry["handle"], protected_records=(changed,),
                                       scope=scope, kind=ResourceKind.CALENDAR)


def test_supported_revocation_cannot_rebind_resource_identity(kit, monkeypatch):
    flow = staged(kit, monkeypatch)
    revoked = deepcopy(revoked_records(flow))
    revoked[0]["record"]["resource_id"] = "another_private_resource"
    with pytest.raises(StateConflict, match="immutable"):
        flow.repo.persist_protected_references(flow.task_id, "org_demo", flow.actor.actor_id, revoked)
    assert all(not record["record"]["revoked"] for record in
               flow.repo.load_protected_references(flow.task_id, "org_demo"))


def test_dispatching_is_pending_in_tal_audit_and_operator_status(kit, monkeypatch):
    from tal.office_journal import OfficeJournalKind, SQLiteOfficeJournal

    flow = staged(kit, monkeypatch)
    with TemporaryDirectory(prefix="safe-reference-audit-") as directory:
        journal = SQLiteOfficeJournal(Path(directory) / "tal.sqlite")
        try:
            pending = SyntheticRecoveryWorker(
                flow.repo, ReferenceBroker(secret=BROKER_KEY), flow.calendar,
                task_adapter=flow.internal,
                tal_dispatch=TrustedTALOfficeDispatchCheck(office_journal=journal),
            )
            with monkeypatch.context() as crash_patch:
                def crash(self, **kwargs):
                    raise InjectedCrash("process exit after admission")
                crash_patch.setattr(FakeCalendarWriter, "create", crash)
                with pytest.raises(InjectedCrash):
                    pending.process_one(task_id=flow.task_id, now=NOW)
            status = kit["coordinator"].execution_status(flow.actor, flow.task_id, now=NOW)
            assert status["status"] == "DISPATCHING"
            assert pending.reconcile_tal_audit(flow.task_id, now=NOW) == "ADMISSION"
            assert pending.audit_sync_warning(flow.task_id) is None
            assert journal.replay(status["action_hash"]).state is OfficeJournalKind.ADMISSION
            assert len(journal.events(status["action_hash"])) == 1
            assert flow.calendar.effect_count == 0
        finally:
            journal.close()


def test_existing_sqlite_migrations_accept_dispatch_and_recovery(kit, monkeypatch, tmp_path):
    path = tmp_path / "existing_head.sqlite"
    migrate(path, "0014_billing_verifier_identity")
    repo = LedgerRepository(sqlite_engine(path))
    scoped = {**kit, "repo": repo,
              "coordinator": TrustedOfficeTasks(repo, kit["office"], OfficeGate(kit["broker"]))}
    try:
        test_crash_after_admission_requires_fresh_authority_when_effect_absent(
            scoped, monkeypatch, revoked=False,
        )
        with repo.engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0014_billing_verifier_identity"
            )
    finally:
        repo.close()
