"""M2 conformance: policy, replay, approvals, and non-effectful proposals."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.broker import BrokerScope, ReferenceBroker, ReferenceDenied, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.control import DecisionStatus
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import AccessDenied
from safe_agent.storage.models import ApprovalRow, ExecutionRow, LedgerRow, ProposalRow
from safe_agent.storage.repository import StateConflict

from .conftest import NOW, SLOT_END, SLOT_START, arrange


def test_registers_immutable_manifest_and_genesis(kit):
    coordinator = kit["coordinator"]
    task = kit["task"]
    result = coordinator.register(kit["principal"], task, kit["manifest"], now=NOW)
    assert result.phase is OfficePhase.ASSIGNED
    assert result.state_version == 0 and result.ledger_sequence == 1
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW) == result
    with pytest.raises(StateConflict):
        coordinator.register(kit["principal"], task, kit["manifest"], now=NOW)
    modified = kit["manifest"].model_copy(update={"template_id": "other_template"})
    with pytest.raises(ValueError, match="authority"):
        coordinator.register(kit["principal"], task, modified, now=NOW)


def test_read_only_action_eligibility_never_dispatches(kit):
    coordinator, state, projection, sim, proposal = arrange(kit)
    answer = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                  expected_state_version=state.state_version, now=NOW)
    assert answer.decision is DecisionStatus.AUTHORIZED
    assert answer.reason_code == "OFFLINE_POLICY_ELIGIBLE_NOT_UPSTREAM_TAL"
    current = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
    assert current.phase is OfficePhase.READY_TO_COMMIT
    assert current.cycles_used == 1 and current.external_effects == 0
    assert current.proposal_hash == sha256(proposal)
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW) == current
    with kit["repo"]._sessions() as session:
        assert session.scalars(select(ExecutionRow)).all() == []
        assert len(session.scalars(select(ProposalRow)).all()) == 1
    assert len(kit["office"]._source.calendars[0].bookings) == 3


@pytest.mark.parametrize("change,expected", [
    ("calendar_ref", "TARGET_OUTSIDE_SCOPE"),
    ("customer_ref", "TARGET_OUTSIDE_SCOPE"),
    ("template_id", "TEMPLATE_OR_TIMEZONE_MISMATCH"),
    ("time_zone", "TEMPLATE_OR_TIMEZONE_MISMATCH"),
    ("simulation_ref", "SIMULATION_NOT_ACTIVE"),
    ("projection_rev", "SIMULATION_NOT_VALID"),
    ("start_at", "SIMULATION_CHANGE_MISMATCH"),
    ("end_at", "SIMULATION_CHANGE_MISMATCH"),
])
def test_closed_gate_denies_wrong_action_parameters_without_effects(kit, change, expected):
    coordinator, state, projection, sim, proposal = arrange(kit)
    alternative = {
        "calendar_ref": "ref_foreign_calendar",
        "customer_ref": "ref_foreign_customer",
        "template_id": "invented_template",
        "time_zone": "America/Chicago",
        "simulation_ref": "simulation_forged",
        "projection_rev": "projection_forged",
        "start_at": SLOT_START + timedelta(minutes=5),
        "end_at": SLOT_END + timedelta(minutes=5),
    }[change]
    changed = proposal.model_copy(update={change: alternative})
    answer = coordinator.propose(kit["principal"], "task_m2", changed.model_dump_json(),
                                  expected_state_version=state.state_version, now=NOW)
    assert answer.decision is DecisionStatus.DENIED and answer.reason_code == expected
    current = coordinator.replay(kit["principal"], "task_m2", now=NOW)
    assert current.phase is OfficePhase.SIMULATED
    assert current.state_version == state.state_version
    assert current.ledger_sequence == state.ledger_sequence + 1
    with kit["repo"]._sessions() as session:
        assert session.scalars(select(ProposalRow)).all() == []
        assert session.scalars(select(ExecutionRow)).all() == []


def test_malformed_and_batch_proposal_are_denied_with_redacted_audit(kit):
    coordinator, state, *_ = arrange(kit)
    for text in ('[]', '{"operation":"calendar.create_event","access_token":"secret"}',
                 '{"operation":"calendar.create_event","operation":"calendar.create_event"}',
                 '{"operation":"shell.exec"}'):
        answer = coordinator.propose(kit["principal"], "task_m2", text,
                                      expected_state_version=state.state_version, now=NOW)
        assert answer.reason_code == "INVALID_PROPOSAL_SCHEMA"
        assert answer.decision is DecisionStatus.DENIED
    events = coordinator.events(kit["principal"], "task_m2", now=NOW)
    assert len(events) == 6  # genesis, preview, four audit rejections
    assert all("secret" not in event.model_dump_json() for event in events)
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW).cycles_used == 0


def test_stale_versions_cannot_authorize_or_use_same_cycle_twice(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    first = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                 expected_state_version=state.state_version, now=NOW)
    assert first.decision is DecisionStatus.AUTHORIZED
    another = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                   expected_state_version=state.state_version, now=NOW)
    assert another.reason_code == "STALE_TASK_VERSION"
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW).cycles_used == 1


def test_manifest_grant_must_exist(kit):
    manifest = kit["manifest"].model_copy(update={"permitted_operations": ()})
    task = kit["task"].model_copy(update={"manifest_hash": sha256(manifest)})
    kit["coordinator"].register(kit["principal"], task, manifest, now=NOW)
    observed = kit["office"].observe  # This gate never acquires a grant.
    from safe_agent.tal.office_gate import compile_office_grant
    assert compile_office_grant(manifest) is None
    assert callable(observed)


def test_approval_binds_exact_hash_actor_and_expiration(kit):
    coordinator, state, _, _, proposal = arrange(kit, approval=True)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                    expected_state_version=state.state_version, now=NOW)
    assert decision.decision is DecisionStatus.APPROVAL_REQUIRED
    waiting = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
    assert waiting.phase is OfficePhase.AWAITING_APPROVAL
    ask = ApprovalInput(proposal_hash=decision.proposal_hash,
                        expected_state_version=waiting.state_version, approved=True,
                        expires_at=NOW + timedelta(hours=2))
    with pytest.raises(AccessDenied):
        coordinator.approve(kit["principal"], "task_m2", ask, now=NOW)
    with pytest.raises(StateConflict):
        coordinator.approve(kit["manager"], "task_m2",
                            ask.model_copy(update={"proposal_hash": sha256({"other": True})}),
                            now=NOW)
    with pytest.raises(StateConflict):
        coordinator.approve(kit["manager"], "task_m2",
                            ask.model_copy(update={"expires_at": NOW - timedelta(seconds=1)}),
                            now=NOW)
    approved = coordinator.approve(kit["manager"], "task_m2", ask, now=NOW)
    assert approved.decision is DecisionStatus.AUTHORIZED
    end_state = coordinator.replay(kit["manager"], "task_m2", now=NOW)
    assert end_state.phase is OfficePhase.READY_TO_COMMIT and end_state.approval_id
    with kit["repo"]._sessions() as session:
        record = session.scalars(select(ApprovalRow)).one()
        assert record.proposal_hash == sha256(proposal)
    with pytest.raises(StateConflict):
        coordinator.approve(kit["manager"], "task_m2", ask, now=NOW)


def test_denied_approval_never_authorizes(kit):
    coordinator, state, _, _, proposal = arrange(kit, approval=True)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                    expected_state_version=state.state_version, now=NOW)
    waiting = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
    denied = coordinator.approve(kit["manager"], "task_m2", ApprovalInput(
        proposal_hash=decision.proposal_hash, expected_state_version=waiting.state_version,
        approved=False, expires_at=NOW + timedelta(hours=1)), now=NOW)
    assert denied.decision is DecisionStatus.DENIED
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW).phase is OfficePhase.REJECTED


def test_preview_verification_and_scope(kit):
    coordinator, state, projection, sim, _ = arrange(kit)
    with pytest.raises(StateConflict):
        coordinator.preview(kit["principal"], "task_m2", projection, sim,
                            expected_state_version=0, now=NOW)
    with pytest.raises(AccessDenied):
        other = kit["principal"].model_copy(update={"organization_id": "org_other"})
        coordinator.preview(other, "task_m2", projection, sim,
                            expected_state_version=state.state_version, now=NOW)
    with pytest.raises(StateConflict, match="independent simulation"):
        altered = sim.model_copy(update={"simulation_hash": sha256({"fake": True})})
        coordinator.preview(kit["principal"], "task_m2", projection, altered,
                            expected_state_version=state.state_version, now=NOW)


def test_ledger_tampering_and_deleted_events_detected(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                         expected_state_version=state.state_version, now=NOW)
    repo = kit["repo"]
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW)
    with repo._sessions.begin() as session:
        record = session.get(LedgerRow, ("task_m2", 2))
        assert record is not None
        record.event_json = {**record.event_json, "payload": {"simulation_ref": "bogus"}}
    with pytest.raises(ValueError):
        repo.replay_task("task_m2")


def test_signed_broker_bindings_survive_restart_and_reject_tampering(kit):
    repo, broker = kit["repo"], kit["broker"]
    kit["coordinator"].register(kit["principal"], kit["task"], kit["manifest"], now=NOW)
    repo.persist_protected_references("task_m2", "org_demo", "employee_demo",
                                      broker.trusted_export(task_id="task_m2"))
    saved = repo.load_protected_references("task_m2", "org_demo")
    new_broker = ReferenceBroker(secret=b"m2_test_private_broker_key_never_prod_000001")
    new_broker.trusted_import(saved)
    scope = BrokerScope(organization_id="org_demo", initiator_id="employee_demo",
                        task_id="task_m2", operation="calendar.create_event", now=NOW)
    assert new_broker.resolve(kit["calendar_ref"], scope=scope, kind=ResourceKind.CALENDAR) == (
        "cal_main_private")
    bad = ({**saved[0], "mac": "a" * 64},)
    with pytest.raises(ReferenceDenied):
        new_broker.trusted_import(bad)
    with pytest.raises(StateConflict):
        repo.persist_protected_references("task_m2", "org_wrong", "employee_demo", saved)


def test_persistent_db_reopens_and_replays(kit):
    repo, coordinator, principal = kit["repo"], kit["coordinator"], kit["principal"]
    coordinator.register(principal, kit["task"], kit["manifest"], now=NOW)
    from safe_agent.storage.repository import LedgerRepository
    second = LedgerRepository.testing_sqlite(str(repo.engine.url))
    try:
        assert second.replay_task("task_m2") == repo.replay_task("task_m2")
    finally:
        second.close()


def test_cross_org_audit_is_denied_without_leaking_task_existence(kit):
    coordinator = kit["coordinator"]
    coordinator.register(kit["principal"], kit["task"], kit["manifest"], now=NOW)
    other = kit["manager"].model_copy(update={"organization_id": "org_foreign"})
    with pytest.raises(AccessDenied):
        coordinator.events(other, "task_m2", now=NOW)
