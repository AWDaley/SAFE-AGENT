"""Synthetic relational fixtures only: these rows are NOT runtime admission.

No provider, secrets, mutable money-account helper or live task API is used.
The small parent rows let the real migration/FKs be tested independently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection

from safe_agent.canonical import sha256
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationBinding,
    seal_invocation_event,
)
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    PlanningAttemptRow,
    TaskRow,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def binding(key: str = "a", *, task_key: str | None = None,
            attempt_key: str | None = None) -> InferenceInvocationBinding:
    task_key = task_key or key
    attempt_key = attempt_key or key
    return InferenceInvocationBinding(
        request_id="inf_" + (key * 32)[:32], organization_id="org_" + task_key,
        task_id="task_" + task_key, attempt_id="attempt_" + attempt_key,
        task_state_version=0, workspace_version=1,
        manifest_hash=sha256({"synthetic_manifest": task_key}),
        route_id="offline_route", model_revision="offline_model_v1",
        prompt_digest=sha256({"synthetic_prompt": task_key}),
        request_digest=sha256({"synthetic_request": key}),
        issued_at=NOW, deadline_at=NOW + timedelta(seconds=10),
        lease_expires_at=NOW + timedelta(seconds=90),
    )


def seed_parents(
    conn: Connection, bound: InferenceInvocationBinding, *,
    task: bool = True, attempt: bool = True, reservation: bool = True,
) -> None:
    if task:
        conn.execute(TaskRow.__table__.insert().values(
            task_id=bound.task_id, organization_id=bound.organization_id,
            initiator_id="synthetic_actor", manifest_id="synthetic_manifest",
            manifest_hash=bound.manifest_hash, task_json={}, snapshot_json={},
            state_version=0, ledger_sequence=0, created_at=NOW,
            deadline=NOW + timedelta(hours=1),
        ))
    if attempt:
        conn.execute(PlanningAttemptRow.__table__.insert().values(
            attempt_id=bound.attempt_id, task_id=bound.task_id, attempt_number=1,
            task_state_version=bound.task_state_version, manifest_hash=bound.manifest_hash,
            mode="text_only", started_at=NOW, lease_until=bound.lease_expires_at,
        ))
    if reservation:
        conn.execute(InferenceBudgetReservationRow.__table__.insert().values(
            request_id=bound.request_id, organization_id=bound.organization_id,
            task_id=bound.task_id, route_id=bound.route_id,
            model_revision=bound.model_revision, prompt_digest=bound.prompt_digest,
            backend_kind="REMOTE", max_microusd=10, held_microusd=10, charged_microusd=0,
            status="IN_FLIGHT", reserved_at=NOW, deadline_at=bound.deadline_at,
            event_sequence=1, latest_event_hash=sha256({"synthetic_budget": bound.request_id}),
            updated_at=NOW,
        ))


def initial_values(bound: InferenceInvocationBinding) -> dict[str, object]:
    event = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    return {
        **bound.model_dump(exclude={"schema_version"}), "binding_hash": bound.binding_hash,
        "state": "RESERVED", "state_version": 0, "claim_version": 0,
        "claim_owner": None, "claim_until": None, "dispatch_started_at": None,
        "proposal_hash": None, "reason_code": None, "event_sequence": 1,
        "latest_event_hash": event.event_hash, "updated_at": NOW,
    }
