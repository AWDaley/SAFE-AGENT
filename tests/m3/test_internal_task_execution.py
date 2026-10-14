"""M3 tasks.create uses exactly the same proposal/gate/ledger/intent path."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.broker import BrokerScope, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.control import DecisionStatus, ExecutionOutcome, TaskBudget
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal, InternalTaskManifest, InternalTaskRecord,
)
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.storage.models import ExecutionRow
from safe_agent.storage.repository import StateConflict
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office.models import InternalTaskChange, ObserveTasks
from tests.m2.conftest import NOW, kit  # noqa: F401


def make_task_flow(kit, *, approval=False, task_id="task_internal"):
    broker, office, coordinator = kit["broker"], kit["office"], kit["coordinator"]
    scope = BrokerScope(
        organization_id="org_demo", initiator_id="employee_demo", task_id=task_id,
        operation="tasks.create", now=NOW,
    )
    customer_ref = broker.mint(
        resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
        scope=scope, permitted_operations=("tasks.create",), expires_at=NOW + timedelta(days=7),
    )
    due = NOW + timedelta(days=3)
    manifest = InternalTaskManifest(
        manifest_id="manifest_internal", manifest_version=1, task_id=task_id,
        organization_id="org_demo", initiator_id="employee_demo",
        customer_ref=customer_ref, task_code="prepare_documents",
        permitted_operations=("tasks.create",),
        allowed_due_start=NOW + timedelta(days=1), allowed_due_end=NOW + timedelta(days=6),
        requires_approval=approval, budget=TaskBudget(max_cycles=3),
    )
    task = InternalTaskRecord(
        task_id=task_id, organization_id="org_demo", initiator_id="employee_demo",
        manifest_id=manifest.manifest_id, manifest_hash=sha256(manifest),
        created_at=NOW - timedelta(minutes=10), deadline=NOW + timedelta(days=2),
    )
    coordinator.register(kit["principal"], task, manifest, now=NOW)
    projection = office.observe_tasks(ObserveTasks(
        organization_id="org_demo", initiator_id="employee_demo", task_id=task_id,
        customer_ref=customer_ref,
    ), as_of=NOW)
    change = InternalTaskChange(customer_ref=customer_ref, task_code="prepare_documents", due_at=due)
    simulation = office.simulate_task(projection, change, actor="employee_demo", now=NOW)
    assert simulation.conflicts == ()
    prepared = coordinator.preview_internal_task(
        kit["principal"], task_id, projection, simulation,
        expected_state_version=0, now=NOW,
    )
    proposal = InternalTaskCreateProposal(
        customer_ref=customer_ref, task_code=change.task_code, due_at=due,
        simulation_ref=simulation.simulation_ref, projection_rev=projection.revision,
    )
    decision = coordinator.propose(
        kit["principal"], task_id, proposal.model_dump_json(),
        expected_state_version=prepared.state_version, now=NOW,
    )
    assert decision.decision is (
        DecisionStatus.APPROVAL_REQUIRED if approval else DecisionStatus.AUTHORIZED
    )
    if approval:
        waiting = coordinator.inspect(kit["principal"], task_id, now=NOW)
        granted = coordinator.approve(kit["manager"], task_id, ApprovalInput(
            proposal_hash=decision.proposal_hash,
            expected_state_version=waiting.state_version,
            approved=True, expires_at=NOW + timedelta(hours=1),
        ), now=NOW)
        assert granted.decision is DecisionStatus.AUTHORIZED
    adapter = FakeInternalTaskAdapter(office._source)
    gateway = SyntheticExecutionGateway(
        kit["repo"], office, OfficeGate(broker), broker,
        FakeCalendarAdapter(office._source), task_adapter=adapter,
    )
    coordinator._synthetic_gateway = gateway
    caller = kit["principal"].model_copy(update={
        "scopes": (*kit["principal"].scopes, "task:execute"),
    })
    return coordinator, adapter, caller, task_id, proposal


@pytest.mark.parametrize("approval", [False, True])
def test_task_create_is_independently_verified_under_same_gate(kit, approval):
    coordinator, adapter, caller, task_id, proposal = make_task_flow(kit, approval=approval)
    before = adapter.reader().snapshot(organization_id="org_demo")
    snap = coordinator.inspect(caller, task_id, now=NOW)
    assert snap.phase is OfficePhase.READY_TO_COMMIT
    receipt = coordinator.execute_synthetic(
        caller, task_id, expected_state_version=snap.state_version, now=NOW,
    )
    assert receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    source = adapter.reader().snapshot(organization_id="org_demo")
    assert source.revision != before.revision
    assert receipt.effect_ref is not None
    task = adapter.reader().task(organization_id="org_demo", task_ref=receipt.effect_ref)
    assert task is not None
    assert task.task_code == proposal.task_code and task.due_at == proposal.due_at
    completed = coordinator.replay(caller, task_id, now=NOW)
    assert completed.phase is OfficePhase.COMPLETED and completed.external_effects == 1
    assert completed.verification_hash and completed.effect_ref == receipt.effect_ref
    assert coordinator.events(caller, task_id, now=NOW)[-1].kind is EventKind.ACTION_VERIFIED
    with kit["repo"]._sessions() as session:
        record = session.scalars(select(ExecutionRow)).one()
        assert record.outcome == "VERIFIED"
    with pytest.raises(StateConflict):
        coordinator.execute_synthetic(caller, task_id,
                                      expected_state_version=completed.state_version, now=NOW)
    assert adapter.effect_count == 1
    evidence = "".join(e.model_dump_json() for e in coordinator.events(caller, task_id, now=NOW))
    assert "cust_c1" not in evidence and "@" not in evidence


def test_stale_internal_task_source_rejected_before_intent(kit):
    coordinator, adapter, caller, task_id, _ = make_task_flow(kit)
    source = adapter.synthetic_state()
    adapter.replace_source_for_test(source.model_copy(update={
        "task_list_revision": "other_task_revision",
    }))
    snap = coordinator.inspect(caller, task_id, now=NOW)
    with pytest.raises(StateConflict, match="source changed"):
        coordinator.execute_synthetic(caller, task_id,
                                      expected_state_version=snap.state_version, now=NOW)
    assert adapter.effect_count == 0
    assert coordinator.inspect(caller, task_id, now=NOW) == snap


def test_unverified_task_write_is_not_completed(kit):
    coordinator, adapter, caller, task_id, _ = make_task_flow(kit)
    adapter.inject_readback_failure(True)
    snap = coordinator.inspect(caller, task_id, now=NOW)
    receipt = coordinator.execute_synthetic(caller, task_id,
                                            expected_state_version=snap.state_version, now=NOW)
    assert receipt.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 1
    replayed = coordinator.replay(caller, task_id, now=NOW)
    assert replayed.phase is OfficePhase.NEEDS_REVIEW
    assert replayed.external_effects == 0
    with pytest.raises(StateConflict):
        coordinator.execute_synthetic(caller, task_id,
                                      expected_state_version=replayed.state_version, now=NOW)


def test_unauthorized_internal_task_operation_cannot_admit(kit):
    coordinator, adapter, caller, task_id, _ = make_task_flow(kit)
    # A rejected unknown operation never enters the effect gateway.
    current = coordinator.inspect(caller, task_id, now=NOW)
    answer = coordinator.propose(kit["principal"], task_id,
        '{"operation":"tasks.delete","target":"all"}',
        expected_state_version=current.state_version, now=NOW)
    assert answer.decision is DecisionStatus.DENIED
    assert adapter.effect_count == 0
