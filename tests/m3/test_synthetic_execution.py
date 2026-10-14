"""M3 verified fake calendar effects through the existing M2 task authority plane."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter, FakeCalendarConflict
from safe_agent.contracts.control import DecisionStatus, ExecutionOutcome
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import AccessDenied
from safe_agent.storage.models import ExecutionRow
from safe_agent.storage.repository import StateConflict
from safe_agent.tal.office_gate import OfficeGate
from tests.m2.conftest import NOW, SLOT_END, SLOT_START, arrange, kit  # noqa: F401


def install(kit):
    adapter = FakeCalendarAdapter(kit["office"]._source)
    gateway = SyntheticExecutionGateway(
        kit["repo"], kit["office"], OfficeGate(kit["broker"]), kit["broker"], adapter,
    )
    kit["coordinator"]._synthetic_gateway = gateway
    execute_as = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    return adapter, execute_as


def ready(kit, *, approval=False):
    coordinator, state, projection, simulation, proposal = arrange(kit, approval=approval)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                   expected_state_version=state.state_version, now=NOW)
    if approval:
        assert decision.decision is DecisionStatus.APPROVAL_REQUIRED
        waiting = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
        grant = coordinator.approve(kit["manager"], "task_m2", ApprovalInput(
            proposal_hash=decision.proposal_hash,
            expected_state_version=waiting.state_version,
            approved=True, expires_at=NOW + timedelta(hours=1),
        ), now=NOW)
        assert grant.decision is DecisionStatus.AUTHORIZED
    else:
        assert decision.decision is DecisionStatus.AUTHORIZED
    return coordinator, proposal


@pytest.mark.parametrize("approval", [False, True])
def test_verified_commit_changes_fake_calendar_and_replays(kit, approval):
    coordinator, proposal = ready(kit, approval=approval)
    adapter, caller = install(kit)
    before = adapter.reader().snapshot(calendar_id="cal_main_private", organization_id="org_demo")
    initial_count = len(before.calendar.bookings)
    phase = coordinator.inspect(caller, "task_m2", now=NOW)
    assert phase.phase is OfficePhase.READY_TO_COMMIT
    receipt = coordinator.execute_synthetic(caller, "task_m2",
                                            expected_state_version=phase.state_version, now=NOW)
    assert receipt.outcome is ExecutionOutcome.COMMITTED
    assert receipt.effect_ref is not None and receipt.verification_ref is not None
    assert adapter.effect_count == 1
    after = adapter.reader().snapshot(calendar_id="cal_main_private", organization_id="org_demo")
    assert len(after.calendar.bookings) == initial_count + 1
    assert before.calendar.source_revision != after.calendar.source_revision
    booking = adapter.reader().booking(
        calendar_id="cal_main_private", organization_id="org_demo", event_ref=receipt.effect_ref,
    )
    assert booking is not None
    assert booking.start_at == proposal.start_at and booking.end_at == proposal.end_at
    snapshot = coordinator.replay(caller, "task_m2", now=NOW)
    assert snapshot.phase is OfficePhase.COMPLETED
    assert snapshot.external_effects == 1
    assert snapshot.effect_ref == receipt.effect_ref and snapshot.verification_hash
    assert coordinator.inspect(caller, "task_m2", now=NOW) == snapshot
    assert coordinator.events(caller, "task_m2", now=NOW)[-2].kind is EventKind.ACTION_INTENT_RECORDED
    assert coordinator.events(caller, "task_m2", now=NOW)[-1].kind is EventKind.ACTION_VERIFIED
    with kit["repo"]._sessions() as session:
        execution = session.scalars(select(ExecutionRow)).one()
        assert execution.outcome == "VERIFIED"
        assert execution.proposal_hash == snapshot.proposal_hash
    with pytest.raises(StateConflict):
        coordinator.execute_synthetic(caller, "task_m2",
                                      expected_state_version=snapshot.state_version, now=NOW)
    assert adapter.effect_count == 1
    trace = "".join(event.model_dump_json() for event in coordinator.events(caller, "task_m2", now=NOW))
    assert "cal_main_private" not in trace and "cust_c1" not in trace
    assert "@" not in trace


def test_no_dispatch_before_authorization_or_from_unprivileged_principal(kit):
    adapter, caller = install(kit)
    coordinator, _, _, _, proposal = arrange(kit)
    pending = coordinator.inspect(caller, "task_m2", now=NOW)
    with pytest.raises(StateConflict):
        coordinator.execute_synthetic(caller, "task_m2",
                                      expected_state_version=pending.state_version, now=NOW)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                   expected_state_version=pending.state_version, now=NOW)
    assert decision.decision is DecisionStatus.AUTHORIZED
    current = coordinator.inspect(caller, "task_m2", now=NOW)
    with pytest.raises(AccessDenied):
        coordinator.execute_synthetic(kit["principal"], "task_m2",
                                      expected_state_version=current.state_version, now=NOW)
    with pytest.raises(AccessDenied):
        coordinator.execute_synthetic(kit["manager"].model_copy(update={
            "scopes": (*kit["manager"].scopes, "task:execute"),
        }), "task_m2", expected_state_version=current.state_version, now=NOW)
    assert adapter.effect_count == 0
    assert coordinator.replay(caller, "task_m2", now=NOW).phase is OfficePhase.READY_TO_COMMIT


def test_changed_authoritative_source_fails_before_intent(kit):
    coordinator, _ = ready(kit)
    adapter, caller = install(kit)
    before = coordinator.inspect(caller, "task_m2", now=NOW)
    source = adapter.synthetic_state()
    cal = source.calendar("cal_main_private")
    corrupted = cal.model_copy(update={"source_revision": "rev_someone_else"})
    adapter.replace_source_for_test(source.model_copy(update={
        "calendars": tuple(corrupted if c.calendar_id == cal.calendar_id else c
                           for c in source.calendars),
    }))
    with pytest.raises(StateConflict, match="source has changed"):
        coordinator.execute_synthetic(caller, "task_m2",
                                      expected_state_version=before.state_version, now=NOW)
    assert adapter.effect_count == 0
    assert coordinator.inspect(caller, "task_m2", now=NOW) == before
    with kit["repo"]._sessions() as session:
        assert session.scalars(select(ExecutionRow)).all() == []


def test_readback_failure_never_marks_completion_and_never_retries(kit):
    coordinator, _ = ready(kit)
    adapter, caller = install(kit)
    adapter.inject_readback_failure(True)
    current = coordinator.inspect(caller, "task_m2", now=NOW)
    receipt = coordinator.execute_synthetic(caller, "task_m2",
                                            expected_state_version=current.state_version, now=NOW)
    assert receipt.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert receipt.verification_ref is None
    assert adapter.effect_count == 1  # write may have happened; read-back failed
    replayed = coordinator.replay(caller, "task_m2", now=NOW)
    assert replayed.phase is OfficePhase.NEEDS_REVIEW
    assert replayed.external_effects == 0 and replayed.verification_hash is None
    assert coordinator.events(caller, "task_m2", now=NOW)[-1].kind is EventKind.ACTION_UNVERIFIED
    with pytest.raises(StateConflict):
        coordinator.execute_synthetic(caller, "task_m2",
                                      expected_state_version=replayed.state_version, now=NOW)
    assert adapter.effect_count == 1


def test_expired_approval_and_no_self_approval_cannot_commit(kit):
    coordinator, _ = ready(kit, approval=True)
    adapter, caller = install(kit)
    current = coordinator.inspect(caller, "task_m2", now=NOW)
    with pytest.raises(StateConflict, match="approval no longer"):
        coordinator.execute_synthetic(caller, "task_m2",
                                      expected_state_version=current.state_version,
                                      now=NOW + timedelta(hours=1))
    assert adapter.effect_count == 0
    assert coordinator.inspect(caller, "task_m2", now=NOW) == current


def test_model_cannot_force_completion_via_proposal(kit):
    coordinator, state, *_ = arrange(kit)
    adapter, caller = install(kit)
    malformed = '{"operation":"agent.complete","outcome":"COMMITTED"}'
    decision = coordinator.propose(kit["principal"], "task_m2", malformed,
                                   expected_state_version=state.state_version, now=NOW)
    assert decision.decision is DecisionStatus.DENIED
    assert adapter.effect_count == 0
    assert coordinator.replay(caller, "task_m2", now=NOW).phase is OfficePhase.SIMULATED


def test_fake_adapter_explicitly_rejects_duplicate_action(kit):
    adapter, _ = install(kit)
    snapshot = adapter.reader().snapshot(calendar_id="cal_main_private", organization_id="org_demo")
    ref = adapter.trusted_writer().create(
        calendar_id="cal_main_private", organization_id="org_demo", customer_id="cust_c1",
        start_at=SLOT_START, end_at=SLOT_END, template_id="follow_up_v1",
        expected_revision=snapshot.calendar.source_revision, expected_source_hash=snapshot.source_hash,
        action_hash="sha256:" + "a" * 64,
    )
    assert ref and adapter.effect_count == 1
    with pytest.raises(FakeCalendarConflict):
        adapter.trusted_writer().create(
            calendar_id="cal_main_private", organization_id="org_demo", customer_id="cust_c1",
            start_at=SLOT_START, end_at=SLOT_END, template_id="follow_up_v1",
            expected_revision=snapshot.calendar.source_revision,
            expected_source_hash=snapshot.source_hash,
            action_hash="sha256:" + "a" * 64,
        )
    assert adapter.effect_count == 1


def test_receipt_tampering_is_detected_by_replay(kit):
    coordinator, _ = ready(kit)
    _, caller = install(kit)
    current = coordinator.inspect(caller, "task_m2", now=NOW)
    receipt = coordinator.execute_synthetic(caller, "task_m2",
                                            expected_state_version=current.state_version, now=NOW)
    assert receipt.outcome is ExecutionOutcome.COMMITTED
    with kit["repo"]._sessions.begin() as session:
        record = session.scalars(select(ExecutionRow)).one()
        record.receipt_json = {**record.receipt_json, "effect_ref": "fake_event_forged"}
    with pytest.raises(ValueError, match="verified receipt/evidence mismatch"):
        kit["repo"].replay_task("task_m2")
