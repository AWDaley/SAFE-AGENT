"""Optional fail-closed TAL-owned calendar admission *comparison* for synthetic tasks.

This bridge preserves the existing single SAFE-AGENT trusted coordinator. It
requires the upstream TAL `tal.office` module and independently checks policy
and evidence before accepting a simulated calendar proposal. It is neither a
`TALRuntime.apply_proposal` bridge nor authority to dispatch to Google.
"""

from __future__ import annotations

from datetime import datetime

from safe_agent.broker import ReferenceBroker
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import (
    ActionDecision,
    DecisionStatus,
    Task,
    TaskManifest,
)
from safe_agent.contracts.internal import (
    InternalTaskCreateProposal,
    InternalTaskManifest,
    InternalTaskRecord,
)
from safe_agent.evidence.ledger import TaskSnapshot
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office.models import (
    CalendarProjection,
    CalendarSimulation,
    InternalTaskSimulation,
    TaskListProjection,
)


class UpstreamOfficePolicyGate(OfficeGate):
    """Require TAL agreement before local synthetic calendar admission.

    If TAL cannot import, hash or validate, the proposal is denied; there is no
    bypass or fallback. Internal tasks remain *synthetic-only* under the existing
    local policy until TAL publishes that separate domain.
    """

    def __init__(self, broker: ReferenceBroker) -> None:
        super().__init__(broker)

    def evaluate(
        self, *, task: Task | InternalTaskRecord,
        manifest: TaskManifest | InternalTaskManifest,
        snapshot: TaskSnapshot,
        proposal: CalendarCreateEvent | InternalTaskCreateProposal,
        projection: CalendarProjection | TaskListProjection,
        simulation: CalendarSimulation | InternalTaskSimulation,
        now: datetime,
    ) -> ActionDecision:
        local = super().evaluate(
            task=task, manifest=manifest, snapshot=snapshot,
            proposal=proposal, projection=projection, simulation=simulation, now=now,
        )
        if local.decision is DecisionStatus.DENIED:
            return local
        if isinstance(proposal, InternalTaskCreateProposal):
            return local  # No TAL internal-task domain; remains synthetic-only.

        def denied(reason: str) -> ActionDecision:
            return ActionDecision(
                decision=DecisionStatus.DENIED, manifest_hash=task.manifest_hash,
                proposal_hash=sha256(proposal), reason_code=reason,
                state_version=snapshot.state_version,
            )

        if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
                or not isinstance(projection, CalendarProjection)
                or not isinstance(simulation, CalendarSimulation)):
            return denied("TAL_DOMAIN_CONTRACT_MISMATCH")
        try:
            from tal.core import sha256 as tal_sha256  # type: ignore[import-not-found,import-untyped]
            from tal.office import (  # type: ignore[import-not-found,import-untyped]
                OfficeAdmissionStatus,
                OfficeCalendarGrantDraft,
                OfficeCalendarProposal,
                TrustedOfficeCalendarEvidence,
                admit_office_calendar,
                compile_office_calendar_grant,
            )

            # SHA equivalence is required; incompatible canonicalizers cannot
            # silently produce approvals under different object identities.
            if tal_sha256(manifest.model_dump(mode="json", exclude_none=True)) != task.manifest_hash:
                return denied("TAL_MANIFEST_CANONICALIZATION_MISMATCH")
            if tal_sha256(proposal.model_dump(mode="json", exclude_none=True)) != sha256(proposal):
                return denied("TAL_PROPOSAL_CANONICALIZATION_MISMATCH")
            grant = compile_office_calendar_grant(OfficeCalendarGrantDraft(
                organization_id=manifest.organization_id,
                task_id=manifest.task_id,
                initiator_id=manifest.initiator_id,
                source_manifest_hash=task.manifest_hash,
                source_manifest_version=manifest.manifest_version,
                calendar_ref=manifest.calendar_ref,
                customer_ref=manifest.customer_ref,
                template_id=manifest.template_id,
                allowed_start=manifest.allowed_start,
                allowed_end=manifest.allowed_end,
                time_zone=manifest.time_zone,
                max_duration_minutes=manifest.max_duration_minutes,
                max_cycles=manifest.budget.max_cycles,
                max_external_effects=manifest.budget.max_external_effects,
                requires_approval=manifest.requires_approval,
                task_deadline=task.deadline,
            ))
            upstream_proposal = OfficeCalendarProposal(
                organization_id=task.organization_id,
                task_id=task.task_id, initiator_id=task.initiator_id,
                source_manifest_hash=task.manifest_hash,
                source_proposal_hash=sha256(proposal),
                calendar_ref=proposal.calendar_ref,
                customer_ref=proposal.customer_ref,
                template_id=proposal.template_id,
                start_at=proposal.start_at, end_at=proposal.end_at,
                time_zone=proposal.time_zone,
                projection_rev=proposal.projection_rev,
                simulation_ref=proposal.simulation_ref,
            )
            change = simulation.change_diff.after
            evidence = TrustedOfficeCalendarEvidence(
                organization_id=task.organization_id,
                task_id=task.task_id, initiator_id=task.initiator_id,
                source_manifest_hash=task.manifest_hash,
                source_proposal_hash=sha256(proposal),
                projection_rev=projection.revision,
                projection_expires_at=projection.expires_at,
                simulation_ref=simulation.simulation_ref,
                simulation_projection_rev=simulation.projection_rev,
                simulation_conflicts=tuple(simulation.conflicts),
                expected_postconditions=tuple(simulation.expected_postconditions),
                projected_calendar_ref=change.calendar_ref,
                projected_customer_ref=change.customer_ref,
                projected_template_id=change.template_id,
                projected_start_at=change.start_at,
                projected_end_at=change.end_at,
                phase="SIMULATED", state_version=snapshot.state_version,
                cycles_used=snapshot.cycles_used,
                effects_used=snapshot.external_effects,
            )
            decision = admit_office_calendar(grant, upstream_proposal, evidence, now=now)
            # TAL's preliminary decision does not confer any execution authority.
            if decision.authorizes_execution is not False:
                return denied("TAL_UNEXPECTED_EXECUTION_AUTHORITY")
            if decision.status is OfficeAdmissionStatus.DENIED:
                return denied("TAL_OFFICE_POLICY_DENIED")
            if (local.decision is DecisionStatus.AUTHORIZED
                    and decision.status is not OfficeAdmissionStatus.ELIGIBLE):
                return denied("TAL_APPROVAL_STATUS_MISMATCH")
            if (local.decision is DecisionStatus.APPROVAL_REQUIRED
                    and decision.status is not OfficeAdmissionStatus.APPROVAL_REQUIRED):
                return denied("TAL_APPROVAL_STATUS_MISMATCH")
            return local
        except (ImportError, ValueError, TypeError, AttributeError):
            return denied("TAL_OFFICE_ADMISSION_UNAVAILABLE")
