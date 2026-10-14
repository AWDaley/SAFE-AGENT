"""Trusted Google test-calendar intent staging. NO Google write or dispatcher.

The original M4 worker remains exact-synthetic-only. This service binds a
single, already approved SAFE office proposal to a server-owned test connection,
a fresh sanitized Google free/busy observation, a TAL authority record and the
same durable SQL outbox/ledger. There is deliberately NO runnable Google worker.
"""
from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Literal

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import DecisionStatus, Task, TaskManifest
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.repository import LedgerRepository, StateConflict, TaskTransaction

from .service import GoogleTestCalendarService


class StagedGoogleTestIntent(StrictModel):
    """Safe receipt for an unclaimed, non-executable Google test outbox intent."""

    task_id: Identifier
    action_hash: Digest
    provider_kind: Literal["google_test"] = "google_test"
    status: Literal["QUEUED_NO_PROVIDER_WORKER"] = "QUEUED_NO_PROVIDER_WORKER"
    provider_write_enabled: Literal[False] = False


def _trusted_tal_authorization(
    tx: TaskTransaction, *, action_hash: str, now: datetime,
) -> object:
    """Rebuild the TAL command solely from persisted immutable task state."""
    try:
        tal_core = import_module("tal.core")
        tal_office = import_module("tal.office")
        tal_runtime = import_module("tal.runtime")
        tal_adapters = import_module("tal.adapters")
        tal_storage = import_module("tal.storage")
        import_module("tal.office_execution")
        task, manifest, snap = tx.task, tx.manifest, tx.snapshot
        proposal = tx.read_active_proposal()
        if (not isinstance(task, Task)
                or not isinstance(manifest, TaskManifest)
                or not isinstance(proposal, CalendarCreateEvent)
                or snap.phase is not OfficePhase.EXECUTING
                or snap.proposal_hash != sha256(proposal)
                or manifest.task_id != task.task_id
                or sha256(manifest) != task.manifest_hash):
            raise StateConflict("TAL_TRUSTED_GOOGLE_CONTEXT_INVALID")
        projection, simulation = tx.preview(proposal.simulation_ref)
        if (proposal.projection_rev != projection.revision
                or simulation.projection_rev != projection.revision
                or simulation.simulation_ref != proposal.simulation_ref
                or simulation.conflicts
                or simulation.expected_postconditions != (
                    "event_created", "matching_time")):
            raise StateConflict("TAL_TRUSTED_GOOGLE_SIMULATION_INVALID")
        # Match the existing upstream synthetic conformance bridge exactly.
        for local in (manifest, proposal):
            if tal_core.sha256(local.model_dump(
                mode="json", exclude_none=True,
            )) != sha256(local):
                raise StateConflict("TAL_GOOGLE_CANONICALIZATION_MISMATCH")
        grant = tal_office.compile_office_calendar_grant(
            tal_office.OfficeCalendarGrantDraft(
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
        )
        upstream_proposal = tal_office.OfficeCalendarProposal(
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
        approval = tx.read_approval(snap.approval_id) if snap.approval_id else None
        tal_approval = None
        if approval is not None:
            tal_approval = tal_office.OfficeApprovalEvidence(
                approved=approval.approved,
                approver_id=approval.approver_id,
                manifest_hash=approval.manifest_hash,
                proposal_hash=approval.proposal_hash,
                expires_at=approval.expires_at,
            )
        change = simulation.change_diff.after
        evidence = tal_office.TrustedOfficeCalendarEvidence(
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
            phase="EXECUTING",
            state_version=snap.state_version,
            cycles_used=snap.cycles_used,
            effects_used=snap.external_effects,
            approval=tal_approval,
        )
        command = tal_office.OfficeRuntimeAction(
            grant=grant, proposal=upstream_proposal, evidence=evidence,
            expected_state_version=snap.state_version,
            outbox_action_hash=action_hash,
        )
        # No tool access, external credentials or shadow journal are installed.
        runtime = tal_runtime.TALRuntime(
            tal_storage.InMemoryEventStore(), tal_adapters.FakeToolRegistry(),
            clock=lambda: now,
        )
        return runtime.prepare_office_execution_authorization(
            task.task_id, command, actor_id=task.initiator_id,
        )
    except StateConflict:
        raise
    except Exception as error:
        raise StateConflict("TAL_GOOGLE_AUTHORIZATION_UNAVAILABLE") from error


class GoogleTestOutboxStager:
    """Privileged-only interface; never register as agent/model or HTTP tool."""

    def __init__(
        self, repo: LedgerRepository, broker: ReferenceBroker,
        coordinator: TrustedOfficeTasks, service: GoogleTestCalendarService,
    ) -> None:
        if type(service) is not GoogleTestCalendarService:
            raise TypeError("test Google service requires the reviewed concrete type")
        self._repo, self._broker = repo, broker
        self._coordinator, self._service = coordinator, service

    def stage(
        self, *, principal: Principal, task_id: str, connection_ref: str,
        expected_state_version: int, now: datetime,
    ) -> StagedGoogleTestIntent:
        """Store one non-executable action atomically after a real free/busy READ.

        Staging an action cannot create any event in Google. The future PR3
        worker must revalidate all source and TAL conditions before new writes.
        """
        now = require_aware(now)
        for permission in ("task:read", "task:execute", "connection:read"):
            principal.require(permission, principal.organization_id, now)  # type: ignore[arg-type]
        # Local simulation remains advisory. Do not treat the synthetic source
        # as provider authority; a separate fresh Google observation is required.
        ready = self._coordinator.revalidate_offline_readiness(
            principal, task_id, now=now,
        )
        if ready.decision is not DecisionStatus.AUTHORIZED:
            raise StateConflict("GOOGLE_LOCAL_PROPOSAL_NOT_READY")
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            if (not isinstance(task, Task)
                    or not isinstance(manifest, TaskManifest)
                    or principal.actor_id != task.initiator_id
                    or principal.organization_id != task.organization_id
                    or snap.state_version != expected_state_version
                    or snap.phase is not OfficePhase.READY_TO_COMMIT
                    or snap.proposal_hash is None):
                raise AccessDenied("GOOGLE_TASK_SCOPE_OR_VERSION_DENIED")
            proposal = tx.read_active_proposal()
            if (not isinstance(proposal, CalendarCreateEvent)
                    or sha256(proposal) != snap.proposal_hash
                    or proposal.calendar_ref != manifest.calendar_ref
                    or proposal.customer_ref != manifest.customer_ref):
                raise StateConflict("GOOGLE_PROPOSAL_BINDING_INVALID")
        # Never accept a caller-supplied model-visible observation as evidence.
        # The scoped OAuth service performs a genuine provider read (mocked in CI).
        observed = self._service.observe(
            principal=principal, connection_ref=connection_ref,
            task_id=task_id, calendar_ref=manifest.calendar_ref,
            from_at=proposal.start_at, until_at=proposal.end_at, now=now,
        )
        if (observed.advisory_only is not True
                or observed.from_at != proposal.start_at
                or observed.until_at != proposal.end_at
                or not any(slot.start_at <= proposal.start_at
                           and proposal.end_at <= slot.end_at
                           for slot in observed.free_slots)):
            raise StateConflict("GOOGLE_OBSERVED_SLOT_UNAVAILABLE")
        with self._repo.write_task(task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            if (not isinstance(task, Task)
                    or not isinstance(manifest, TaskManifest)
                    or snap.phase is not OfficePhase.READY_TO_COMMIT
                    or snap.state_version != expected_state_version
                    or snap.proposal_hash != sha256(proposal)
                    or now >= task.deadline
                    or snap.external_effects >= manifest.budget.max_external_effects
                    or snap.cycles_used > manifest.budget.max_cycles
                    or now < observed.fetched_at or now >= observed.expires_at):
                raise StateConflict("GOOGLE_STALE_TRUSTED_STATE")
            active = tx.read_active_proposal()
            if active != proposal:
                raise StateConflict("GOOGLE_ACTIVE_PROPOSAL_CHANGED")
            approval = tx.read_approval(snap.approval_id) if snap.approval_id else None
            if manifest.requires_approval and (
                approval is None or not approval.approved
                or approval.approver_id == task.initiator_id
                or approval.manifest_hash != task.manifest_hash
                or approval.proposal_hash != snap.proposal_hash
                or now >= approval.expires_at
            ):
                raise StateConflict("GOOGLE_APPROVAL_NOT_VALID")
            scope = BrokerScope(
                organization_id=task.organization_id,
                initiator_id=task.initiator_id, task_id=task_id,
                operation="calendar.create_event", now=now,
            )
            self._broker.resolve(
                manifest.calendar_ref, scope=scope, kind=ResourceKind.CALENDAR,
            )
            self._broker.resolve(
                manifest.customer_ref, scope=scope, kind=ResourceKind.CUSTOMER,
            )
            action_hash = sha256({
                "task_id": task_id, "manifest_hash": task.manifest_hash,
                "proposal_hash": snap.proposal_hash, "cycle": snap.cycles_used,
            })
            request_hash = sha256({
                "provider": "google_test", "action_hash": action_hash,
                "manifest_hash": task.manifest_hash,
                "proposal_hash": snap.proposal_hash,
                "connection_ref": connection_ref,
                "calendar_ref": manifest.calendar_ref,
                "start_at": proposal.start_at, "end_at": proposal.end_at,
                "time_zone": proposal.time_zone,
            })
            tx.store_protected_references(
                self._broker.trusted_export(task_id=task_id),
            )
            tx.write_execution_intent(
                action_hash, snap.proposal_hash, now=now,
                operation="calendar.create_event",
                source_revision=observed.revision,
                source_hash=sha256(observed),
                provider_kind="google_test",
            )
            tx.append(
                EventKind.ACTION_INTENT_RECORDED,
                payload={
                    "proposal_hash": snap.proposal_hash,
                    "action_hash": action_hash,
                },
                actor="trusted_google_test_stager", now=now,
            )
            authority = _trusted_tal_authorization(
                tx, action_hash=action_hash, now=now,
            )
            tx.persist_office_execution_authorization(authority, now=now)
            tx.bind_google_test_action(
                action_hash=action_hash, connection_ref=connection_ref,
                observation=observed, request_hash=request_hash, now=now,
            )
        return StagedGoogleTestIntent(task_id=task_id, action_hash=action_hash)
