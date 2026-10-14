"""M4 durable fake-provider recovery, expiry, idempotency and ledger invariants."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.broker import ReferenceBroker
from safe_agent.contracts.control import ExecutionOutcome
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.storage.models import FakeProviderEffectRow, OutboxRow
from safe_agent.storage.provider import DurableFakeProvider, ProviderIntegrityError
from safe_agent.storage.repository import LedgerRepository, StateConflict
from safe_agent.tal.office_gate import OfficeGate
from tests.m2.conftest import NOW, arrange
from tests.m3.test_internal_task_execution import make_task_flow
from tests.m3.test_synthetic_execution import ready

BROKER_SECRET = b"m2_test_private_broker_key_never_prod_000001"


def setup_calendar(kit):
    coordinator, _ = ready(kit)
    provider = DurableFakeProvider(kit["repo"].engine, kit["office"]._source)
    adapter = FakeCalendarAdapter(kit["office"]._source, durable_store=provider)
    gateway = SyntheticExecutionGateway(kit["repo"], kit["office"],
                                        OfficeGate(kit["broker"]), kit["broker"], adapter)
    caller = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    return coordinator, adapter, gateway, caller


def reboot_calendar(kit, tmp_path, source):
    # The old SQL engine/session/adapter are all replaced; provider state isn't re-seeded.
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'm2.sqlite'}")
    provider = DurableFakeProvider(repo.engine, source)
    cal = FakeCalendarAdapter(source, durable_store=provider)
    broker = ReferenceBroker(secret=BROKER_SECRET)
    return repo, cal, broker, SyntheticRecoveryWorker(repo, broker, cal)


def stage_calendar(kit):
    _, adapter, gateway, caller = setup_calendar(kit)
    version = kit["repo"].read_task("task_m2").state_version
    action_hash = gateway.enqueue(caller, "task_m2", now=NOW,
                                  expected_state_version=version)
    assert kit["repo"].read_task("task_m2").phase is OfficePhase.EXECUTING
    assert adapter.effect_count == 0
    assert kit["repo"].read_outbox("task_m2")["status"] == "QUEUED"
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.EXECUTING
    return action_hash, adapter


def test_durable_calendar_normal_dispatch(kit):
    _, adapter, gateway, caller = setup_calendar(kit)
    receipt = gateway.execute(caller, "task_m2", now=NOW,
                              expected_state_version=kit["repo"].read_task("task_m2").state_version)
    assert receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].read_outbox("task_m2")["status"] == "VERIFIED"
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED


@pytest.mark.parametrize("fault", [FaultPoint.BEFORE_DISPATCH, FaultPoint.AFTER_PROVIDER_COMMIT])
def test_reboot_after_crash_does_not_duplicate_calendar_booking(kit, tmp_path, fault):
    action_hash, adapter = stage_calendar(kit)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    with pytest.raises(InjectedCrash):
        worker.process_one(task_id="task_m2", now=NOW, fault=fault)
    initial_effects = adapter.effect_count
    assert initial_effects == (0 if fault is FaultPoint.BEFORE_DISPATCH else 1)
    original = kit["office"]._source
    kit["repo"].close()
    repo, restored_adapter, _, restored_worker = reboot_calendar(kit, tmp_path, original)
    try:
        assert restored_adapter.effect_count == initial_effects
        # The lease prohibits premature retry.
        assert restored_worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=1)) is None
        receipt = restored_worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=31))
        assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
        assert receipt.action_hash == action_hash
        assert restored_adapter.effect_count == 1
        assert repo.replay_task("task_m2").phase is OfficePhase.COMPLETED
        assert restored_worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=80)) is None
    finally:
        repo.close()


def test_response_loss_enters_unknown_then_reconciles_without_new_effect(kit):
    stage_hash, adapter = stage_calendar(kit)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    result = worker.process_one(task_id="task_m2", now=NOW, fault=FaultPoint.RESPONSE_LOST)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert kit["repo"].read_task("task_m2").phase is OfficePhase.EXECUTING
    assert kit["repo"].read_outbox("task_m2")["status"] == "OUTCOME_UNKNOWN"
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.EXECUTING
    assert adapter.effect_count == 1
    final = worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=6))
    assert final is not None and final.outcome is ExecutionOutcome.COMMITTED
    assert final.action_hash == stage_hash
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED


def test_stale_source_and_expired_approval_do_not_dispatch(kit):
    action_hash, adapter = stage_calendar(kit)
    source = adapter.synthetic_state()
    changed = source.calendar("cal_main_private").model_copy(update={
        "source_revision": "external_revision",
    })
    adapter._durable.apply(
        organization_id="org_demo", action_hash="sha256:" + "f" * 64,
        operation="calendar.create_event", request_hash="sha256:" + "e" * 64,
        mutation=lambda current: (
            current.model_copy(update={
                "calendars": tuple(changed if c.calendar_id == changed.calendar_id else c
                                   for c in current.calendars)
            }), "other_synthetic_effect",
        ),
    )
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    result = worker.process_one(task_id="task_m2", now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 1  # only the unrelated concurrent effect, never our booking
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert kit["repo"].read_outbox("task_m2")["status"] == "NEEDS_REVIEW"
    assert action_hash != "sha256:" + "f" * 64


def test_bounded_unknown_without_effect_escalates_after_provider_errors(kit):
    _, adapter = stage_calendar(kit)
    adapter.inject_readback_failure(True)
    # A provider effect actually succeeded, but its readback never succeeds.
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter, max_attempts=2)
    first = worker.process_one(task_id="task_m2", now=NOW)
    assert first is not None and first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    second = worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=6))
    assert second is not None and second.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert adapter.effect_count == 1


def test_provider_durable_idempotency_recognizes_exact_payload(kit):
    _, adapter, gateway, caller = setup_calendar(kit)
    version = kit["repo"].read_task("task_m2").state_version
    gateway.execute(caller, "task_m2", now=NOW, expected_state_version=version)
    with kit["repo"]._sessions() as session:
        effect = session.scalars(select(FakeProviderEffectRow).where(
            FakeProviderEffectRow.operation == "calendar.create_event")).one()
    assert adapter.find_effect(organization_id="org_demo", action_hash=effect.action_hash) == effect.effect_ref
    with pytest.raises(ProviderIntegrityError):
        adapter.find_effect(organization_id="org_demo", action_hash=effect.action_hash,
                            request_hash="sha256:" + "b" * 64)


def test_internal_task_durable_recovery_and_restart(kit, tmp_path):
    _, _, caller, task_id, _ = make_task_flow(kit)
    source = kit["office"]._source
    provider = DurableFakeProvider(kit["repo"].engine, source)
    durable_tasks = FakeInternalTaskAdapter(source, durable_store=provider)
    gateway = SyntheticExecutionGateway(
        kit["repo"], kit["office"], OfficeGate(kit["broker"]), kit["broker"],
        FakeCalendarAdapter(source, durable_store=provider), task_adapter=durable_tasks,
    )
    current = kit["repo"].read_task(task_id)
    action_hash = gateway.enqueue(caller, task_id, now=NOW,
                                  expected_state_version=current.state_version)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"],
                                     FakeCalendarAdapter(source, durable_store=provider),
                                     task_adapter=durable_tasks)
    with pytest.raises(InjectedCrash):
        worker.process_one(task_id=task_id, now=NOW, fault=FaultPoint.AFTER_PROVIDER_COMMIT)
    assert durable_tasks.effect_count == 1
    kit["repo"].close()
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'm2.sqlite'}")
    try:
        restarted = DurableFakeProvider(repo.engine, source)
        new_tasks = FakeInternalTaskAdapter(source, durable_store=restarted)
        recover = SyntheticRecoveryWorker(
            repo, ReferenceBroker(secret=BROKER_SECRET),
            FakeCalendarAdapter(source, durable_store=restarted), task_adapter=new_tasks,
        )
        result = recover.process_one(task_id=task_id, now=NOW + timedelta(seconds=31))
        assert result is not None and result.action_hash == action_hash
        assert result.outcome is ExecutionOutcome.COMMITTED
        assert new_tasks.effect_count == 1
        assert repo.replay_task(task_id).phase is OfficePhase.COMPLETED
    finally:
        repo.close()


def test_outbox_tamper_is_detected_by_replay(kit):
    stage_calendar(kit)
    with kit["repo"]._sessions.begin() as session:
        row = session.scalars(select(OutboxRow)).one()
        row.proposal_hash = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="outbox not bound"):
        kit["repo"].replay_task("task_m2")


def test_worker_lease_excludes_parallel_claims_and_old_owner(kit):
    stage_calendar(kit)
    first = kit["repo"].claim_action(worker_id="worker_one", now=NOW)
    assert first is not None and first.attempts == 1
    assert kit["repo"].claim_action(worker_id="worker_two", now=NOW) is None
    assert kit["repo"].claim_action(worker_id="worker_two", now=NOW + timedelta(seconds=29)) is None
    second = kit["repo"].claim_action(worker_id="worker_two", now=NOW + timedelta(seconds=31))
    assert second is not None and second.attempts == 2
    assert second.preceding_status == "LEASED"
    with pytest.raises(StateConflict, match="no longer owns"):
        kit["repo"].release_action_unknown(first, now=NOW + timedelta(seconds=31),
                                            reason="STALE_WORKER")
    assert kit["repo"].read_outbox("task_m2")["status"] == "LEASED"


def test_expired_approval_blocks_first_dispatch_even_with_queued_intent(kit):
    coordinator, _, _, _, proposal = arrange(kit, approval=True)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                   expected_state_version=1, now=NOW)
    waiting = kit["repo"].read_task("task_m2")
    from safe_agent.orchestration.office_tasks import ApprovalInput
    coordinator.approve(kit["manager"], "task_m2", ApprovalInput(
        proposal_hash=decision.proposal_hash, expected_state_version=waiting.state_version,
        approved=True, expires_at=NOW + timedelta(seconds=10),
    ), now=NOW)
    source = kit["office"]._source
    provider = DurableFakeProvider(kit["repo"].engine, source)
    adapter = FakeCalendarAdapter(source, durable_store=provider)
    gateway = SyntheticExecutionGateway(kit["repo"], kit["office"],
                                        OfficeGate(kit["broker"]), kit["broker"], adapter)
    caller = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    gateway.enqueue(caller, "task_m2", now=NOW,
                    expected_state_version=kit["repo"].read_task("task_m2").state_version)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    result = worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=12))
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 0
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW


def test_rate_limit_does_not_write_and_recovers_after_backoff(kit):
    _, adapter = stage_calendar(kit)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    result = worker.process_one(task_id="task_m2", now=NOW, fault=FaultPoint.RATE_LIMIT)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 0
    assert kit["repo"].read_outbox("task_m2")["status"] == "OUTCOME_UNKNOWN"
    assert worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=10)) is None
    final = worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=16))
    assert final is not None and final.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1


def test_no_hidden_customer_data_in_outbox_and_ledger(kit):
    stage_calendar(kit)
    with kit["repo"]._sessions() as session:
        row = session.scalars(select(OutboxRow)).one()
        private_columns = " ".join(str(getattr(row, col.name)) for col in row.__table__.columns)
    assert "cal_main_private" not in private_columns
    assert "cust_c1" not in private_columns
    assert "@" not in private_columns
    events = "".join(e.model_dump_json() for e in kit["repo"].read_events("task_m2"))
    assert "cal_main_private" not in events and "cust_c1" not in events
