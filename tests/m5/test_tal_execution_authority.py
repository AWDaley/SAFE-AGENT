"""TAL office authority remains separate from effect dispatch."""
from __future__ import annotations

import pytest
from sqlalchemy import update

from safe_agent.canonical import sha256
from safe_agent.storage.models import OfficeExecutionAuthorizationRow
from safe_agent.storage.repository import StateConflict
from tests.m2.conftest import NOW
from tests.m4.test_recovery import stage_calendar

office = pytest.importorskip("tal.office")
tal = pytest.importorskip("tal.office_execution")
runtime = pytest.importorskip("tal.runtime")
adapters = pytest.importorskip("tal.adapters")
storage = pytest.importorskip("tal.storage")


def build(kit, action_hash):
    with kit["repo"].write_task("task_m2") as tx:
        task, manifest, snap = tx.task, tx.manifest, tx.snapshot
        proposal = tx.read_active_proposal()
        projection, sim = tx.preview(proposal.simulation_ref)
        grant = office.compile_office_calendar_grant(office.OfficeCalendarGrantDraft(
            organization_id=task.organization_id,
            task_id=task.task_id, initiator_id=task.initiator_id,
            source_manifest_hash=task.manifest_hash,
            source_manifest_version=manifest.manifest_version,
            calendar_ref=manifest.calendar_ref, customer_ref=manifest.customer_ref,
            template_id=manifest.template_id, allowed_start=manifest.allowed_start,
            allowed_end=manifest.allowed_end, time_zone=manifest.time_zone,
            max_duration_minutes=manifest.max_duration_minutes,
            max_cycles=manifest.budget.max_cycles,
            max_external_effects=manifest.budget.max_external_effects,
            requires_approval=manifest.requires_approval, task_deadline=task.deadline,
        ))
        change = sim.change_diff.after
        p = office.OfficeCalendarProposal(
            organization_id=task.organization_id, task_id=task.task_id,
            initiator_id=task.initiator_id, source_manifest_hash=task.manifest_hash,
            source_proposal_hash=sha256(proposal),
            calendar_ref=proposal.calendar_ref, customer_ref=proposal.customer_ref,
            template_id=proposal.template_id, start_at=proposal.start_at,
            end_at=proposal.end_at, time_zone=proposal.time_zone,
            projection_rev=proposal.projection_rev, simulation_ref=proposal.simulation_ref,
        )
        evidence = office.TrustedOfficeCalendarEvidence(
            organization_id=task.organization_id, task_id=task.task_id,
            initiator_id=task.initiator_id, source_manifest_hash=task.manifest_hash,
            source_proposal_hash=sha256(proposal), projection_rev=projection.revision,
            projection_expires_at=projection.expires_at, simulation_ref=sim.simulation_ref,
            simulation_projection_rev=sim.projection_rev,
            simulation_conflicts=tuple(sim.conflicts),
            expected_postconditions=tuple(sim.expected_postconditions),
            projected_calendar_ref=change.calendar_ref,
            projected_customer_ref=change.customer_ref,
            projected_template_id=change.template_id,
            projected_start_at=change.start_at, projected_end_at=change.end_at,
            phase="EXECUTING", state_version=snap.state_version,
            cycles_used=snap.cycles_used, effects_used=snap.external_effects,
        )
        cmd = office.OfficeRuntimeAction(
            grant=grant, proposal=p, evidence=evidence,
            expected_state_version=snap.state_version,
            outbox_action_hash=action_hash,
        )
    engine = runtime.TALRuntime(
        storage.InMemoryEventStore(), adapters.FakeToolRegistry(), clock=lambda: NOW)
    return engine.prepare_office_execution_authorization(
        task.task_id, cmd, actor_id=task.initiator_id), cmd, engine


def test_persists_and_validates_exact_outbox(kit):
    action, adapter = stage_calendar(kit)
    authority, cmd, engine = build(kit, action)
    with kit["repo"].write_task("task_m2") as tx:
        tx.persist_office_execution_authorization(authority, now=NOW)
    assert kit["repo"].read_office_execution_authorization("task_m2", action) == authority
    engine.require_office_execution_authorization(
        authority, cmd, actor_id="employee_demo",
        state_version=cmd.evidence.state_version)
    assert adapter.effect_count == 0


def test_wrong_tenant_and_duplicate_are_rejected(kit):
    action, adapter = stage_calendar(kit)
    authority, _, _ = build(kit, action)
    forged = authority.model_copy(update={"organization_id": "other_org"})
    with pytest.raises(StateConflict):
        with kit["repo"].write_task("task_m2") as tx:
            tx.persist_office_execution_authorization(forged, now=NOW)
    with kit["repo"].write_task("task_m2") as tx:
        tx.persist_office_execution_authorization(authority, now=NOW)
    with pytest.raises(StateConflict):
        with kit["repo"].write_task("task_m2") as tx:
            tx.persist_office_execution_authorization(authority, now=NOW)
    assert adapter.effect_count == 0


def test_atomic_rollback_and_tamper(kit):
    action, adapter = stage_calendar(kit)
    authority, _, _ = build(kit, action)
    with pytest.raises(RuntimeError):
        with kit["repo"].write_task("task_m2") as tx:
            tx.persist_office_execution_authorization(authority, now=NOW)
            raise RuntimeError("injected crash before commit")
    with pytest.raises(StateConflict):
        kit["repo"].read_office_execution_authorization("task_m2", action)
    with kit["repo"].write_task("task_m2") as tx:
        tx.persist_office_execution_authorization(authority, now=NOW)
    with kit["repo"]._sessions.begin() as session:
        session.execute(update(OfficeExecutionAuthorizationRow).where(
            OfficeExecutionAuthorizationRow.action_hash == action,
        ).values(authorization_hash=sha256("wrong")))
    with pytest.raises(StateConflict):
        kit["repo"].read_office_execution_authorization("task_m2", action)
    assert adapter.effect_count == 0
