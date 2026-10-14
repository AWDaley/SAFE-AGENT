"""Attack-oriented tests: revoked refs, expired projections and audit-only denial."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.contracts.control import DecisionStatus
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.security.principal import AccessDenied
from safe_agent.storage.models import ExecutionRow, ProposalRow

from .conftest import NOW, arrange


def test_revoked_reference_denies_even_when_simulation_previously_valid(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    kit["broker"].revoke(kit["customer_ref"])
    reply = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                 expected_state_version=state.state_version, now=NOW)
    assert reply.decision is DecisionStatus.DENIED
    assert reply.reason_code == "REFERENCE_SCOPE_DENIED"
    assert coordinator.replay(kit["principal"], "task_m2", now=NOW).phase is OfficePhase.SIMULATED


def test_expired_projection_is_denied_before_approval_or_state_change(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                    expected_state_version=state.state_version,
                                    now=NOW + timedelta(minutes=11))
    assert decision.decision is DecisionStatus.DENIED
    assert decision.reason_code == "STALE_PROJECTION"


def test_wrong_operator_scope_and_deadline_denied(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    manager = kit["manager"]
    with pytest.raises(AccessDenied):
        coordinator.propose(manager, "task_m2", proposal.model_dump_json(),
                             expected_state_version=state.state_version, now=NOW)
    expired_actor = kit["principal"].model_copy(update={
        "authenticated_until": NOW - timedelta(seconds=1)})
    with pytest.raises(AccessDenied):
        coordinator.propose(expired_actor, "task_m2", proposal.model_dump_json(),
                             expected_state_version=state.state_version, now=NOW)
    with kit["repo"]._sessions() as session:
        assert not session.scalars(select(ExecutionRow)).all()
        assert not session.scalars(select(ProposalRow)).all()


def test_excessive_duration_and_different_target_cannot_grant(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    long = proposal.model_copy(update={"end_at": proposal.start_at + timedelta(minutes=90)})
    assert coordinator.propose(
        kit["principal"], "task_m2", long.model_dump_json(),
        expected_state_version=state.state_version, now=NOW,
    ).reason_code == "APPOINTMENT_OUTSIDE_AUTHORITY"
    change = proposal.model_copy(update={"calendar_ref": "ref_known_to_model"})
    assert coordinator.propose(kit["principal"], "task_m2", change.model_dump_json(),
                                expected_state_version=state.state_version,
                                now=NOW).reason_code == "TARGET_OUTSIDE_SCOPE"


def test_denial_does_not_increment_projection_version_or_consume_effect_budget(kit):
    coordinator, state, _, _, proposal = arrange(kit)
    bad = proposal.model_copy(update={"template_id": "not_permitted"})
    for i in range(4):
        answer = coordinator.propose(kit["principal"], "task_m2", bad.model_dump_json(),
                                      expected_state_version=state.state_version, now=NOW)
        assert answer.decision is DecisionStatus.DENIED
    replayed = coordinator.replay(kit["principal"], "task_m2", now=NOW)
    assert replayed.ledger_sequence == state.ledger_sequence + 4
    assert replayed.state_version == state.state_version
    assert replayed.cycles_used == 0 and replayed.external_effects == 0
