"""Provisional *synthetic-only* office-domain TAL policy adapter.

This is NOT upstream TAL v0.3 authorization; its only positive result is
an eligibility decision for an offline workflow. No external dispatcher may use
this gate until upstream calendar pack compatibility has been implemented and
certified. UnconfiguredTALAuthority remains deny-all for real actions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from safe_agent.broker import BrokerScope, ReferenceBroker, ReferenceDenied, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import ActionDecision, DecisionStatus, Task, TaskManifest
from safe_agent.evidence.ledger import OfficePhase, TaskSnapshot
from safe_agent.virtual_office.models import CalendarProjection, CalendarSimulation


class CompiledOfficeGrant(StrictModel):
    schema_version: Literal["safe_agent.office_grant.v1"] = "safe_agent.office_grant.v1"
    manifest_hash: Digest
    organization_id: Identifier
    task_id: Identifier
    initiator_id: Identifier
    calendar_ref: Identifier
    customer_ref: Identifier
    allowed_operation: Literal["calendar.create_event"]
    grant_hash: Digest


def compile_office_grant(manifest: TaskManifest) -> CompiledOfficeGrant | None:
    """Compiler is deterministic, model-free and cannot expand the manifest."""
    if "calendar.create_event" not in manifest.permitted_operations:
        return None
    claim = {
        "manifest_hash": sha256(manifest), "organization_id": manifest.organization_id,
        "task_id": manifest.task_id, "initiator_id": manifest.initiator_id,
        "calendar_ref": manifest.calendar_ref, "customer_ref": manifest.customer_ref,
        "allowed_operation": "calendar.create_event",
    }
    return CompiledOfficeGrant(
        manifest_hash=sha256(manifest),
        organization_id=manifest.organization_id,
        task_id=manifest.task_id,
        initiator_id=manifest.initiator_id,
        calendar_ref=manifest.calendar_ref,
        customer_ref=manifest.customer_ref,
        allowed_operation="calendar.create_event",
        grant_hash=sha256(claim),
    )


class OfficeGate:
    """Independent policy validation, outside the planner's context and process."""

    def __init__(self, broker: ReferenceBroker):
        self._broker = broker

    def evaluate(self, *, task: Task, manifest: TaskManifest,
                 snapshot: TaskSnapshot, proposal: CalendarCreateEvent,
                 projection: CalendarProjection, simulation: CalendarSimulation,
                 now: datetime) -> ActionDecision:
        now = require_aware(now)
        proposal_hash = sha256(proposal)

        def deny(code: str) -> ActionDecision:
            return ActionDecision(
                decision=DecisionStatus.DENIED,
                manifest_hash=task.manifest_hash, proposal_hash=proposal_hash,
                reason_code=code, state_version=snapshot.state_version,
            )

        # This policy only establishes local eligibility, NEVER authority to dispatch.
        if (task.manifest_hash != sha256(manifest)
                or manifest.task_id != task.task_id
                or manifest.organization_id != task.organization_id
                or manifest.initiator_id != task.initiator_id
                or snapshot.manifest_hash != task.manifest_hash
                or snapshot.task_id != task.task_id):
            return deny("MANIFEST_OR_TASK_MISMATCH")
        if snapshot.phase is not OfficePhase.SIMULATED:
            return deny("INVALID_TASK_PHASE")
        grant = compile_office_grant(manifest)
        if grant is None:
            return deny("OPERATION_NOT_GRANTED")
        if snapshot.cycles_used >= manifest.budget.max_cycles:
            return deny("CYCLE_BUDGET_EXCEEDED")
        if manifest.budget.max_external_effects < 1:
            return deny("EFFECT_BUDGET_EXCEEDED")
        if now > task.deadline:
            return deny("TASK_DEADLINE_EXPIRED")
        if (proposal.calendar_ref != grant.calendar_ref
                or proposal.customer_ref != grant.customer_ref):
            return deny("TARGET_OUTSIDE_SCOPE")
        if (proposal.template_id != manifest.template_id
                or proposal.time_zone != manifest.time_zone
                or proposal.time_zone != projection.time_zone):
            return deny("TEMPLATE_OR_TIMEZONE_MISMATCH")
        start, end = proposal.start_at.astimezone(UTC), proposal.end_at.astimezone(UTC)
        if (start < manifest.allowed_start.astimezone(UTC)
                or end > manifest.allowed_end.astimezone(UTC)
                or end <= start
                or end - start > timedelta(minutes=manifest.max_duration_minutes)):
            return deny("APPOINTMENT_OUTSIDE_AUTHORITY")
        if now < projection.fetched_at or now >= projection.expires_at:
            return deny("STALE_PROJECTION")
        if (projection.organization_id != task.organization_id
                or projection.task_id != task.task_id
                or projection.calendar_ref != grant.calendar_ref
                or proposal.projection_rev != projection.revision
                or proposal.simulation_ref != simulation.simulation_ref
                or simulation.projection_rev != projection.revision
                or simulation.source_revision != projection.source_revision
                or simulation.conflicts
                or simulation.expected_postconditions != ("event_created", "matching_time")):
            return deny("SIMULATION_NOT_VALID")
        diff = simulation.change_diff.after
        if (proposal.calendar_ref != diff.calendar_ref
                or proposal.customer_ref != diff.customer_ref
                or proposal.start_at != diff.start_at
                or proposal.end_at != diff.end_at
                or proposal.template_id != diff.template_id):
            return deny("SIMULATION_CHANGE_MISMATCH")
        try:
            scope = BrokerScope(
                organization_id=task.organization_id, initiator_id=task.initiator_id,
                task_id=task.task_id, operation="calendar.create_event", now=now,
            )
            self._broker.resolve(grant.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR)
            self._broker.resolve(grant.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER)
        except ReferenceDenied:
            return deny("REFERENCE_SCOPE_DENIED")
        return ActionDecision(
            decision=(DecisionStatus.APPROVAL_REQUIRED if manifest.requires_approval
                      else DecisionStatus.AUTHORIZED),
            manifest_hash=task.manifest_hash,
            proposal_hash=proposal_hash,
            reason_code=("OFFLINE_APPROVAL_REQUIRED" if manifest.requires_approval
                         else "OFFLINE_POLICY_ELIGIBLE_NOT_UPSTREAM_TAL"),
            state_version=snapshot.state_version,
        )
