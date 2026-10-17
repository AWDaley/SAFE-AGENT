"""Durable W4 execution provenance; never infer authority from an advisory cache.

Current execution requires the migrated schema. Absence/corruption of evidence
is not permission to downgrade a coordinated task into a legacy fixture.
"""
from __future__ import annotations

from sqlalchemy import select

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.contracts.coordinated_planning import CoordinatedPlanningInput
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow
from safe_agent.storage.coordinated_planning import bound_result, read_consumption, read_input
from safe_agent.storage.inference_admission import _binding, _history
from safe_agent.storage.models import InferenceInvocationRow, PlanningWorkspaceRow
from safe_agent.storage.planning import _assert_workspace
from safe_agent.storage.repository import StateConflict, TaskTransaction
from safe_agent.virtual_office.models import CalendarProjection


def coordinated_execution_input(
    tx: TaskTransaction, proposal: CalendarCreateEvent, projection: CalendarProjection,
) -> CoordinatedPlanningInput | None:
    """Read immutable provenance under the existing task lock, including on restart.

    None means an actual legacy task with no coordinated input or invocation.
    A returned input makes upstream TAL mandatory for every NEW effect, regardless
    of which worker claimed the outbox. It is not a new execution permission.
    """
    raw = tx.session.scalar(select(CoordinatedPlanningInputRow).where(
        CoordinatedPlanningInputRow.task_id == tx.task.task_id,
    ))
    invocations = tx.session.scalars(select(InferenceInvocationRow).where(
        InferenceInvocationRow.task_id == tx.task.task_id,
    ).limit(2)).all()
    if raw is None:
        if invocations:
            raise StateConflict("COORDINATED_EXECUTION_INPUT_MISSING")
        return None
    value = read_input(raw)
    task, manifest, ctx = tx.task, tx.manifest, value.context
    if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
            or not manifest.requires_approval
            or manifest.permitted_operations != ("calendar.create_event",)
            or sha256(manifest) != task.manifest_hash
            or value.lease.task_id != task.task_id
            or value.lease.manifest_hash != task.manifest_hash
            or ctx.task_id != task.task_id or ctx.organization_id != task.organization_id
            or manifest.initiator_id != task.initiator_id
            or ctx.calendar_ref != manifest.calendar_ref or ctx.customer_ref != manifest.customer_ref
            or ctx.allowed_start != manifest.allowed_start or ctx.allowed_end != manifest.allowed_end
            or ctx.time_zone != manifest.time_zone or ctx.template_id != manifest.template_id
            or projection != value.projection or len(invocations) != 1):
        raise StateConflict("COORDINATED_EXECUTION_BINDING_DENIED")
    workspace = tx.session.get(PlanningWorkspaceRow, task.task_id)
    if workspace is None:
        raise StateConflict("COORDINATED_EXECUTION_WORKSPACE_MISSING")
    _assert_workspace(tx, workspace)
    invocation = invocations[0]
    binding = _binding(invocation)
    history = _history(tx, invocation, binding)
    result = bound_result(tx, value)
    consumed = read_consumption(tx, value)
    if (result.binding != binding or history.state != "RESPONSE_VALIDATED"
            or consumed is None or consumed.result_kind != "PROPOSED"
            or consumed.proposal_hash != sha256(proposal)
            or tx.snapshot.proposal_hash != consumed.proposal_hash):
        raise StateConflict("COORDINATED_EXECUTION_CONSUMPTION_DENIED")
    return value
