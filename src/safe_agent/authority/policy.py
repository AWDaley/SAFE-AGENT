"""Deterministic *provisional* office admission checks.

This is not TAL upstream calendar-domain certification and does not dispatch effects.
All permissions originate in a pinned trusted manifest; proposals convey no authority.
"""

from __future__ import annotations

from datetime import datetime

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import (
    ActionDecision,
    Approval,
    DecisionStatus,
    Task,
    TaskManifest,
    TaskState,
)


class ManifestMismatch(ValueError):
    """Task and manifest fail to match; never authorize."""


def pin_manifest(task: Task, manifest: TaskManifest) -> None:
    if (task.manifest_hash != sha256(manifest)
            or task.manifest_id != manifest.manifest_id
            or task.task_id != manifest.task_id
            or task.organization_id != manifest.organization_id
            or task.initiator_id != manifest.initiator_id):
        raise ManifestMismatch("manifest mismatch")


class OfficeAdmission:
    """Offline simulation admission only; never a provider execution capability."""

    def decide(
        self, task: Task, manifest: TaskManifest, proposal: CalendarCreateEvent,
        *, now: datetime, state_version: int, cycle: int = 1,
        external_effect_count: int = 0, approval: Approval | None = None,
    ) -> ActionDecision:
        now = require_aware(now)
        proposal_hash = sha256(proposal)

        def decision(status: DecisionStatus, reason: str) -> ActionDecision:
            return ActionDecision(
                decision=status, manifest_hash=task.manifest_hash,
                proposal_hash=proposal_hash, reason_code=reason,
                state_version=state_version,
            )

        try:
            pin_manifest(task, manifest)
        except ManifestMismatch:
            return decision(DecisionStatus.DENIED, "MANIFEST_MISMATCH")
        if task.state not in {TaskState.ASSIGNED, TaskState.PLANNING,
                              TaskState.PROPOSED_UNVERIFIED, TaskState.AWAITING_APPROVAL}:
            return decision(DecisionStatus.DENIED, "INVALID_TASK_STATE")
        if now >= task.deadline:
            return decision(DecisionStatus.DENIED, "TASK_EXPIRED")
        if not (1 <= cycle <= manifest.budget.max_cycles):
            return decision(DecisionStatus.DENIED, "CYCLE_BUDGET_EXHAUSTED")
        if external_effect_count >= manifest.budget.max_external_effects:
            return decision(DecisionStatus.DENIED, "EFFECT_BUDGET_EXHAUSTED")
        if proposal.operation not in manifest.permitted_operations:
            return decision(DecisionStatus.DENIED, "CAPABILITY_DENIED")
        if (proposal.calendar_ref != manifest.calendar_ref
                or proposal.customer_ref != manifest.customer_ref
                or proposal.template_id != manifest.template_id):
            return decision(DecisionStatus.DENIED, "RESOURCE_SCOPE_DENIED")
        if (proposal.start_at < manifest.allowed_start
                or proposal.end_at > manifest.allowed_end
                or proposal.start_at <= now):
            return decision(DecisionStatus.DENIED, "TIME_WINDOW_DENIED")
        minutes = (proposal.end_at - proposal.start_at).total_seconds() / 60
        if minutes <= 0 or minutes > manifest.max_duration_minutes:
            return decision(DecisionStatus.DENIED, "DURATION_DENIED")
        if proposal.time_zone != manifest.time_zone:
            return decision(DecisionStatus.DENIED, "TIME_ZONE_DENIED")
        if manifest.requires_approval:
            if approval is None:
                return decision(DecisionStatus.APPROVAL_REQUIRED, "APPROVAL_REQUIRED")
            if (approval.task_id != task.task_id
                    or approval.manifest_hash != task.manifest_hash
                    or approval.proposal_hash != proposal_hash
                    or approval.expires_at <= now
                    or not approval.approved
                    or approval.approver_id == task.initiator_id):
                return decision(DecisionStatus.DENIED, "APPROVAL_INVALID")
        return decision(DecisionStatus.AUTHORIZED, "ADMISSION_ONLY_NO_DISPATCH")
