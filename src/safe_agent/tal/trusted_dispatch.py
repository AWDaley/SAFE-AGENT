"""Trusted, non-executable upstream TAL check for a *leased synthetic* outbox action.

Reconstructs evidence from immutable SAFE-AGENT task and outbox records. It is
not a model tool, a Google write capability, or an alternative effect dispatcher.
"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Approval, Task, TaskManifest
from safe_agent.evidence.ledger import OfficePhase, TaskSnapshot
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_audit import TrustedTALOfficeAuditMirror
from safe_agent.virtual_office.models import CalendarProjection, CalendarSimulation


class TALOfficeDispatchDenied(ValueError):
    """An untrusted/missing upstream TAL runtime cannot authorize dispatch."""


class TrustedTALOfficeDispatchCheck:
    """Mandatory check only when explicitly installed on a synthetic worker.

    A safe new effect still requires the worker's provider-state, broker, lease,
    deadline, budget, and independent read-back checks. TAL's result is never an
    executable permit; `authorizes_execution` must remain False.
    """

    def __init__(self, *, office_journal: object | None = None) -> None:
        # When set, the real upstream runtime journals the admission before a
        # new synthetic effect. Real provider types are still prohibited.
        self._office_journal = office_journal
        self._audit = (TrustedTALOfficeAuditMirror(office_journal)
                       if office_journal is not None else None)

    @property
    def has_audit(self) -> bool:
        return self._audit is not None

    def reconcile_audit(self, repo: LedgerRepository, task_id: str,
                        *, now: datetime) -> str | None:
        if self._audit is None:
            return None
        return self._audit.reconcile(repo, task_id, now=now)

    def require(
        self, *, action_hash: str, task: Task, manifest: TaskManifest,
        proposal: CalendarCreateEvent, projection: CalendarProjection,
        simulation: CalendarSimulation, snapshot: TaskSnapshot,
        approval: Approval | None, now: datetime,
    ) -> None:
        if (snapshot.phase is not OfficePhase.EXECUTING
                or snapshot.task_id != task.task_id
                or snapshot.manifest_hash != task.manifest_hash
                or snapshot.proposal_hash != sha256(proposal)
                or task.manifest_hash != sha256(manifest)
                or manifest.task_id != task.task_id
                or manifest.organization_id != task.organization_id
                or manifest.initiator_id != task.initiator_id
                or proposal.simulation_ref != simulation.simulation_ref
                or proposal.projection_rev != projection.revision
                or simulation.projection_rev != projection.revision
                or simulation.conflicts
                or simulation.expected_postconditions != ("event_created", "matching_time")):
            raise TALOfficeDispatchDenied("TAL_TRUSTED_OUTBOX_CONTEXT_INVALID")

        try:
            upstream_core = import_module("tal.core")
            upstream_office = import_module("tal.office")
            upstream_runtime = import_module("tal.runtime")
            upstream_adapter = import_module("tal.adapters")
            upstream_storage = import_module("tal.storage")
            # This is a version-locked wire contract. Divergent canonicalizers
            # may not agree on what the proposal/manifest actually authorize.
            for local in (manifest, proposal):
                data = local.model_dump(mode="json", exclude_none=True)
                if upstream_core.sha256(data) != sha256(local):
                    raise TALOfficeDispatchDenied("TAL_CANONICALIZATION_MISMATCH")
            draft = upstream_office.OfficeCalendarGrantDraft(
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
            )
            grant = upstream_office.compile_office_calendar_grant(draft)
            upstream_proposal = upstream_office.OfficeCalendarProposal(
                organization_id=task.organization_id,
                task_id=task.task_id,
                initiator_id=task.initiator_id,
                source_manifest_hash=task.manifest_hash,
                source_proposal_hash=sha256(proposal),
                calendar_ref=proposal.calendar_ref,
                customer_ref=proposal.customer_ref,
                template_id=proposal.template_id,
                start_at=proposal.start_at,
                end_at=proposal.end_at,
                time_zone=proposal.time_zone,
                projection_rev=proposal.projection_rev,
                simulation_ref=proposal.simulation_ref,
            )
            change = simulation.change_diff.after
            tal_approval = None
            if approval is not None:
                tal_approval = upstream_office.OfficeApprovalEvidence(
                    approved=approval.approved,
                    approver_id=approval.approver_id,
                    manifest_hash=approval.manifest_hash,
                    proposal_hash=approval.proposal_hash,
                    expires_at=approval.expires_at,
                )
            evidence = upstream_office.TrustedOfficeCalendarEvidence(
                organization_id=task.organization_id,
                task_id=task.task_id,
                initiator_id=task.initiator_id,
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
                phase="EXECUTING", state_version=snapshot.state_version,
                cycles_used=snapshot.cycles_used,
                effects_used=snapshot.external_effects,
                approval=tal_approval,
            )
            command = upstream_office.OfficeRuntimeAction(
                grant=grant, proposal=upstream_proposal, evidence=evidence,
                expected_state_version=snapshot.state_version,
                outbox_action_hash=action_hash,
            )
            options = ({"office_journal": self._office_journal}
                       if self._office_journal is not None else {})
            runtime = upstream_runtime.TALRuntime(
                upstream_storage.InMemoryEventStore(),
                upstream_adapter.FakeToolRegistry(),
                clock=lambda: now, **options,
            )
            decision = runtime.apply_proposal(
                task.task_id, command, actor_id=task.initiator_id,
            )
            if (decision.status is not upstream_office.OfficeAdmissionStatus.ELIGIBLE
                    or decision.authorizes_execution is not False
                    or decision.proposal_hash != upstream_core.sha256(upstream_proposal)
                    or decision.grant_hash != grant.grant_hash
                    or decision.state_version != snapshot.state_version):
                raise TALOfficeDispatchDenied("TAL_OFFICE_RUNTIME_DENIED")
            if self._office_journal is not None:
                upstream_journal = import_module("tal.office_journal")
                if not isinstance(self._office_journal, upstream_journal.SQLiteOfficeJournal):
                    raise TALOfficeDispatchDenied("TAL_OFFICE_JOURNAL_TYPE_INVALID")
                recorded = self._office_journal.replay(action_hash)
                if (recorded.state not in {
                        upstream_journal.OfficeJournalKind.ADMISSION,
                        upstream_journal.OfficeJournalKind.OUTCOME_UNKNOWN,
                    } or self._office_journal.events(action_hash)[0].reason_code != "TRUSTED_ADMISSION_ONLY"
                        or recorded.task_id != task.task_id
                        or recorded.source_manifest_hash != task.manifest_hash
                        or recorded.source_proposal_hash != sha256(proposal)):
                    raise TALOfficeDispatchDenied("TAL_OFFICE_JOURNAL_NOT_ADMITTED")
        except TALOfficeDispatchDenied:
            raise
        except Exception as exc:
            # The trusted extension must fail closed on missing/corrupted TAL or
            # SQLite errors. Never expose provider/SQL details to the model.
            raise TALOfficeDispatchDenied("TAL_OFFICE_RUNTIME_UNAVAILABLE") from exc
