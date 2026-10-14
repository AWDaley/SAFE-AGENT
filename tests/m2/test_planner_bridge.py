"""Exercise the M0/M1 planner through the new M2 trusted policy service."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.canonical import sha256
from safe_agent.contracts.control import DecisionStatus
from safe_agent.orchestration import TaskRunner
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.storage.repository import StateConflict

from .conftest import NOW, SLOT_START, WINDOW_END, arrange


def test_unprivileged_planner_to_trusted_gate_offline_flow(kit):
    coordinator, state, projection, simulation, proposal = arrange(kit)
    context = SafeTaskContext(
        task_id="task_m2", organization_id="org_demo",
        calendar_ref=kit["calendar_ref"], customer_ref=kit["customer_ref"],
        allowed_start=SLOT_START, allowed_end=WINDOW_END, duration_minutes=30,
        time_zone="America/New_York", template_id="follow_up_v1",
        simulation_ref=simulation.simulation_ref,
    )
    planned = TaskRunner(FakePlanner()).plan_once(
        kit["task"], context, projection.to_planner_projection())
    assert planned.proposal == proposal
    decision = coordinator.submit_planning_result(
        kit["principal"], "task_m2", planned,
        expected_state_version=state.state_version, now=NOW)
    assert decision.decision is DecisionStatus.AUTHORIZED
    assert coordinator.revalidate_offline_readiness(
        kit["principal"], "task_m2", now=NOW).reason_code == "OFFLINE_READY_NOT_EXECUTABLE"
    assert coordinator.revalidate_offline_readiness(
        kit["principal"], "task_m2", now=NOW + timedelta(minutes=11)
    ).reason_code == "STALE_PROJECTION"


def test_expired_approval_fails_readiness_even_when_prior_state_ready(kit):
    coordinator, state, _, _, proposal = arrange(kit, approval=True)
    decision = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                    expected_state_version=state.state_version, now=NOW)
    waiting = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
    coordinator.approve(kit["manager"], "task_m2", ApprovalInput(
        proposal_hash=decision.proposal_hash, expected_state_version=waiting.state_version,
        approved=True, expires_at=NOW + timedelta(seconds=20),
    ), now=NOW)
    assert coordinator.revalidate_offline_readiness(
        kit["manager"], "task_m2", now=NOW).decision is DecisionStatus.AUTHORIZED
    assert coordinator.revalidate_offline_readiness(
        kit["manager"], "task_m2", now=NOW + timedelta(seconds=21)
    ).reason_code == "APPROVAL_EXPIRED_OR_INVALID"


def test_model_cannot_forge_bridge_proposal_hash(kit):
    coordinator, state, projection, simulation, proposal = arrange(kit)
    from safe_agent.orchestration.runner import PlanningResult
    result = PlanningResult(task_id="task_m2", proposal=proposal,
                            proposal_hash=sha256({"forged": True}))
    with pytest.raises(StateConflict):
        coordinator.submit_planning_result(kit["principal"], "task_m2", result,
                                            expected_state_version=state.state_version, now=NOW)
