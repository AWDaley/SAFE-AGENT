"""Operator diagnostics and saved-result preparation, with no inference dispatch.

GET does not mutate durable state. Trusted source checks may rebuild in-memory
projection/reference caches. POST uses the existing W4 consumption transaction;
it does not start inference, renew leases, approve, enqueue, or reconcile costs.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.broker.references import ReferenceDenied
from safe_agent.canonical import sha256
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.contracts.coordinated_planning import CoordinatedPlanningInput
from safe_agent.contracts.inference_dispatch import InferenceInvocationView
from safe_agent.contracts.planning_recovery import (
    PlanningRecoveryView,
    RecoveryStatus,
    RetireTaskCommand,
    ResumeSavedResultCommand,
)
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.inference.gateway import _check_candidate
from safe_agent.orchestration.coordinated_planning import TrustedCoordinatedPlanning
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow
from safe_agent.storage.coordinated_planning import bound_result, read_consumption, read_input
from safe_agent.storage.inference_admission import _binding, _history, _live_attempt, _task_identity
from safe_agent.storage.models import (
    InferenceAdmissionAuthorityRow,
    InferenceAdmissionRow,
    InferenceInvocationRow,
    PlanningWorkspaceRow,
)
from safe_agent.storage.planning import _assert_workspace, _utc
from safe_agent.storage.repository import StateConflict, TaskTransaction
from safe_agent.storage.result_models import InferenceValidatedResultRow


class PlanningRecoveryService:
    """Same-tenant readers inspect; only the authorized initiator may resume.

    Diagnostics are observational, not permission to execute. Every resume uses
    the original input and W4's current source, policy, lease and authority checks.
    """

    def __init__(self, planning: TrustedCoordinatedPlanning) -> None:
        if type(planning) is not TrustedCoordinatedPlanning:
            raise TypeError("coordinated planning required")
        self._planning = planning
        self._repo = planning._store._repo

    def _assess(self, tx: TaskTransaction, actor: Principal, now: datetime) -> PlanningRecoveryView:
        task, manifest, snapshot = tx.task, tx.manifest, tx.snapshot
        workspace = tx.session.get(PlanningWorkspaceRow, task.task_id)
        value: CoordinatedPlanningInput | None = None
        invocation: InferenceInvocationView | None = None
        deadline: datetime | None = None
        consumption_hash: str | None = None
        policy_hash = self._planning._inference.store.admission._policy_hash

        def report(
            status: RecoveryStatus, *, resumable: bool = False, retirable: bool = False,
        ) -> PlanningRecoveryView:
            operator = (actor.actor_id == task.initiator_id
                        and "task:prepare" in actor.scopes and "task:propose" in actor.scopes)
            actions: tuple[str, ...] = ("refresh",)
            if operator and resumable and retirable:
                actions = ("refresh", "resume_saved_result", "retire_task")
            elif operator and resumable:
                actions = ("refresh", "resume_saved_result")
            elif operator and retirable:
                actions = ("refresh", "retire_task")
            data = {
                "task_id": task.task_id, "task_phase": snapshot.phase,
                "task_state_version": snapshot.state_version,
                "workspace_version": workspace.workspace_version if workspace else 0,
                "attempts_used": workspace.attempts_used if workspace else 0,
                "status": status,
                "inference_state": invocation.state if invocation else None,
                "inference_deadline": deadline,
                "allowed_actions": actions,
            }
            revision = sha256({
                "view": data, "input": value.input_hash if value else None,
                "invocation": invocation.latest_event_hash if invocation else None,
                "consumption": consumption_hash, "policy": policy_hash,
                "actor": actor.actor_id, "organization": actor.organization_id,
                "scopes": actor.scopes, "authentication_until": actor.authenticated_until,
            })
            return PlanningRecoveryView.model_validate({**data, "recovery_revision": revision})

        if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
                or not manifest.requires_approval
                or manifest.permitted_operations != ("calendar.create_event",)
                or task.manifest_hash != sha256(manifest)):
            return report("BLOCKED")
        if snapshot.phase is OfficePhase.RETIRED:
            return report("TASK_RETIRED")
        if workspace is not None:
            _assert_workspace(tx, workspace)
        raw = tx.session.scalar(select(CoordinatedPlanningInputRow).where(
            CoordinatedPlanningInputRow.task_id == task.task_id,
        ))
        rows = tx.session.scalars(select(InferenceInvocationRow).where(
            InferenceInvocationRow.task_id == task.task_id,
        ).limit(2)).all()
        if raw is None:
            if workspace is None and not rows and snapshot.phase is OfficePhase.ASSIGNED:
                return report("NOT_STARTED", retirable=True)
            return report("INPUT_MISSING", retirable=snapshot.phase is OfficePhase.ASSIGNED)
        value = read_input(raw)
        lease = value.lease
        if (workspace is None or value.context.organization_id != task.organization_id
                or lease.task_id != task.task_id or lease.manifest_hash != task.manifest_hash
                or workspace.mode != "text_only" or workspace.attempts_used != lease.attempt_number
                or value.context.calendar_ref != manifest.calendar_ref
                or value.context.customer_ref != manifest.customer_ref
                or value.context.template_id != manifest.template_id
                or value.context.time_zone != manifest.time_zone
                or value.context.allowed_start != manifest.allowed_start
                or value.context.allowed_end != manifest.allowed_end):
            raise StateConflict("RECOVERY_INTEGRITY_DENIED")
        if value.policy_hash != policy_hash:
            return report("POLICY_CHANGED", retirable=True)
        if len(rows) > 1:
            raise StateConflict("RECOVERY_INTEGRITY_DENIED")
        if rows:
            row = rows[0]
            binding = _binding(row)
            if (binding.task_id != task.task_id or binding.organization_id != task.organization_id
                    or binding.attempt_id != lease.attempt_id or binding.manifest_hash != lease.manifest_hash
                    or binding.task_state_version != lease.task_state_version
                    or binding.workspace_version != lease.workspace_version
                    or binding.lease_expires_at != lease.expires_at
                    or now < _utc(row.updated_at)):
                raise StateConflict("RECOVERY_INTEGRITY_DENIED")
            admitted = tx.session.get(InferenceAdmissionRow, binding.request_id)
            authority = tx.session.get(InferenceAdmissionAuthorityRow, "global")
            if (admitted is None or authority is None or admitted.binding_hash != binding.binding_hash
                    or admitted.policy_hash != value.policy_hash or authority.policy_hash != value.policy_hash
                    or now < _utc(authority.last_seen_at)):
                raise StateConflict("RECOVERY_INTEGRITY_DENIED")
            invocation = _history(tx, row, binding)
            deadline = binding.deadline_at
            stored_result = tx.session.get(InferenceValidatedResultRow, binding.request_id)
            if invocation.state != "RESPONSE_VALIDATED" and stored_result is not None:
                raise StateConflict("RECOVERY_INTEGRITY_DENIED")
            # Deadline expiry alone is NOT evidence that execution or charging
            # was avoided. Keep uncertainty visible rather than implying failure.
            if invocation.state == "UNKNOWN":
                return report("INFERENCE_UNCERTAIN", retirable=True)
            if invocation.state == "DISPATCH_STARTED":
                return report("INFERENCE_PENDING" if now < deadline else "INFERENCE_UNCERTAIN",
                              retirable=now >= deadline)
            if invocation.state == "FAILED_BEFORE_DISPATCH":
                return report("INFERENCE_STOPPED", retirable=True)
            if invocation.state == "RESPONSE_VALIDATED":
                if stored_result is None:
                    return report("RESULT_MISSING", retirable=True)
                record = bound_result(tx, value)
                if record.binding != binding:
                    raise StateConflict("RECOVERY_INTEGRITY_DENIED")
                _check_candidate(record.outcome.proposal, value.context,
                                 value.projection.to_planner_projection())
                consumed = read_consumption(tx, value)
                if consumed is not None:
                    consumption_hash = consumed.consumption_hash
                    if consumed.result_kind == "NEEDS_CLARIFICATION":
                        return report("NEEDS_CLARIFICATION", retirable=True)
                    if snapshot.phase in (OfficePhase.AWAITING_APPROVAL, OfficePhase.READY_TO_COMMIT):
                        return report("PREPARED", retirable=True)
                    return report("TASK_ADVANCED", retirable=snapshot.phase in {
                        OfficePhase.REJECTED, OfficePhase.ASSIGNED,
                    })
        if (now >= task.deadline or now >= lease.expires_at or now >= value.projection.expires_at
                or (deadline is not None and now >= deadline)):
            return report("RESULT_EXPIRED", retirable=True)
        _live_attempt(tx, lease, now)
        try:
            self._planning._references(tx, value)
        except (ReferenceDenied, StateConflict):
            return report("AUTHORITY_CHANGED", retirable=True)
        try:
            self._planning._source(tx, value, now)
        except ReferenceDenied:
            return report("AUTHORITY_CHANGED")
        except ValueError:
            return report("SOURCE_CHANGED", retirable=True)
        if invocation is None:
            return report("INPUT_SAVED", retirable=True)
        if invocation.state == "RESERVED":
            return report("WAITING_TO_DISPATCH", retirable=True)
        if invocation.state != "RESPONSE_VALIDATED":
            raise StateConflict("RECOVERY_INTEGRITY_DENIED")
        return report("RESULT_RECOVERABLE", resumable=True, retirable=True)

    def inspect(self, principal: Principal, task_id: str) -> PlanningRecoveryView:
        actor = Principal.model_validate(principal.model_dump())
        now = self._planning._now()
        with self._repo.write_task(task_id, serialize_sqlite=True) as tx:
            actor.require("task:read", tx.task.organization_id, now)
            result = self._assess(tx, actor, now)
        final = self._planning._now()
        actor.require("task:read", actor.organization_id, final)
        if final < now:
            raise StateConflict("RECOVERY_CLOCK_DENIED")
        if (result.status == "RESULT_RECOVERABLE" and result.inference_deadline is not None
                and final >= result.inference_deadline):
            raise StateConflict("RECOVERY_STATUS_EXPIRED")
        return result

    def resume(self, principal: Principal, task_id: str,
               command: ResumeSavedResultCommand) -> PlanningRecoveryView:
        actor = Principal.model_validate(principal.model_dump())
        body = ResumeSavedResultCommand.model_validate(command.model_dump())
        now = self._planning._now()
        with self._repo.write_task(task_id, serialize_sqlite=True) as tx:
            actor.require("task:read", tx.task.organization_id, now)
            actor.require("task:prepare", tx.task.organization_id, now)
            actor.require("task:propose", tx.task.organization_id, now)
            if actor.actor_id != tx.task.initiator_id:
                raise AccessDenied("recovery initiator required")
            current = self._assess(tx, actor, now)
            if (body.expected_state_version != current.task_state_version
                    or body.expected_workspace_version != current.workspace_version
                    or body.expected_recovery_revision != current.recovery_revision
                    or "resume_saved_result" not in current.allowed_actions):
                raise StateConflict("RECOVERY_REFRESH_REQUIRED")
            raw = tx.session.scalar(select(CoordinatedPlanningInputRow).where(
                CoordinatedPlanningInputRow.task_id == task_id,
            ))
            if raw is None:
                raise StateConflict("RECOVERY_INPUT_MISSING")
            value = read_input(raw)
            _task_identity(tx, actor, value.lease, value.context, now)
        try:
            # Deliberately no call to plan(), inference.execute(), admission or
            # any fallback. W4 repeats its authority/freshness checks under lock.
            self._planning._consume(actor, value)
        except Exception:
            # Inspect exact committed consumption after a lost acknowledgment;
            # never manufacture a completion or start another planning attempt.
            with self._repo.write_task(task_id, serialize_sqlite=True) as tx:
                _task_identity(tx, actor, value.lease, value.context, self._planning._now())
                if read_consumption(tx, value) is None:
                    raise StateConflict("RECOVERY_REFRESH_REQUIRED") from None
        return self.inspect(actor, task_id)


    @staticmethod
    def _saved_prompt(value: CoordinatedPlanningInput) -> str:
        """Rebuild only the exact minimized prompt already bound to admission."""
        return value.prompt()

    def retire(
        self, principal: Principal, task_id: str, command: RetireTaskCommand,
    ) -> PlanningRecoveryView:
        """Terminally retire pre-effect work without erasing evidence or refunding holds."""
        actor = Principal.model_validate(principal.model_dump())
        body = RetireTaskCommand.model_validate(command.model_dump())
        now = self._planning._now()
        value: CoordinatedPlanningInput | None = None
        state: str | None = None
        deadline: datetime | None = None

        with self._repo.write_task(task_id, serialize_sqlite=True) as tx:
            actor.require("task:read", tx.task.organization_id, now)
            actor.require("task:prepare", tx.task.organization_id, now)
            actor.require("task:propose", tx.task.organization_id, now)
            if actor.actor_id != tx.task.initiator_id:
                raise AccessDenied("retirement initiator required")
            current = self._assess(tx, actor, now)
            if (body.expected_state_version != current.task_state_version
                    or body.expected_workspace_version != current.workspace_version
                    or body.expected_recovery_revision != current.recovery_revision
                    or "retire_task" not in current.allowed_actions):
                raise StateConflict("RECOVERY_REFRESH_REQUIRED")
            raw = tx.session.scalar(select(CoordinatedPlanningInputRow).where(
                CoordinatedPlanningInputRow.task_id == task_id,
            ))
            if raw is not None:
                value = read_input(raw)
            rows = tx.session.scalars(select(InferenceInvocationRow).where(
                InferenceInvocationRow.task_id == task_id,
            ).limit(2)).all()
            if len(rows) > 1:
                raise StateConflict("RECOVERY_INTEGRITY_DENIED")
            if rows:
                state = rows[0].state
                deadline = _utc(rows[0].deadline_at)

        # Resolve only lifecycle states that provably permit no future dispatch.
        # These transitions never release a remote-bound budget hold.
        if value is not None and state == "RESERVED":
            self._planning._inference.store.cancel_before_dispatch(
                actor, value.lease, self._saved_prompt(value),
            )
        elif value is not None and state == "DISPATCH_STARTED":
            if deadline is None or now < deadline:
                raise StateConflict("INFERENCE_STILL_PENDING")
            recovered = self._planning._inference.store.recover(
                actor, value.lease, self._saved_prompt(value),
            )
            if recovered.state != "UNKNOWN":
                raise StateConflict("INFERENCE_RECONCILIATION_REQUIRED")

        now = self._planning._now()
        with self._repo.write_task(task_id, serialize_sqlite=True) as tx:
            actor.require("task:read", tx.task.organization_id, now)
            actor.require("task:prepare", tx.task.organization_id, now)
            actor.require("task:propose", tx.task.organization_id, now)
            if (actor.actor_id != tx.task.initiator_id
                    or tx.snapshot.state_version != body.expected_state_version
                    or tx.snapshot.phase not in {
                        OfficePhase.ASSIGNED, OfficePhase.SIMULATED,
                        OfficePhase.PROPOSED_UNVERIFIED, OfficePhase.AWAITING_APPROVAL,
                        OfficePhase.READY_TO_COMMIT, OfficePhase.REJECTED,
                    }):
                raise StateConflict("RECOVERY_REFRESH_REQUIRED")
            workspace = tx.session.get(PlanningWorkspaceRow, task_id)
            if (workspace is None and body.expected_workspace_version != 0) or (
                workspace is not None and workspace.workspace_version
                != body.expected_workspace_version
            ):
                raise StateConflict("RECOVERY_REFRESH_REQUIRED")
            tx.append(
                EventKind.TASK_RETIRED,
                payload={"reason_code": body.reason_code},
                actor=actor.actor_id,
                now=now,
            )
        return self.inspect(actor, task_id)
