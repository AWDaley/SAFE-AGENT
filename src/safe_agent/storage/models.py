"""Trusted persistence mapping; NEVER serialize protected bindings to model context."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "safe_tasks"
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    task_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ledger_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_safe_tasks_org_actor", "organization_id", "initiator_id"),
        Index("uq_safe_tasks_inference_binding", "task_id", "organization_id",
              "manifest_hash", unique=True),
    )


class TaskIntakeRow(Base):
    """Immutable, tenant/actor-scoped idempotency record for trusted task intake."""

    __tablename__ = "safe_task_intake_keys"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    initiator_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("safe_tasks.task_id"), nullable=False, unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class PlanningWorkspaceRow(Base):
    """Mutable planning status, CAS version and bounded task-scoped lease."""

    __tablename__ = "safe_planning_workspaces"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("safe_tasks.task_id"), primary_key=True,
    )
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    workspace_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts_used: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_attempt_id: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reason: Mapped[str | None] = mapped_column(String(64))
    proposal_hash: Mapped[str | None] = mapped_column(String(72))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanningAttemptRow(Base):
    """Immutable attempt issuance, without model text or private observations."""

    __tablename__ = "safe_planning_attempts"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("safe_tasks.task_id"), nullable=False, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    task_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_safe_planning_attempt_order"),
        Index("uq_safe_planning_attempt_inference_binding", "attempt_id", "task_id",
              "manifest_hash", "task_state_version", "lease_until", unique=True),
    )


class PlanningOutcomeRow(Base):
    """Immutable sealed result metadata; proposal contents stay in M2 ledger."""

    __tablename__ = "safe_planning_outcomes"
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("safe_planning_attempts.attempt_id"), primary_key=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("safe_tasks.task_id"), nullable=False,
    )
    result_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    proposal_hash: Mapped[str | None] = mapped_column(String(72))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome_hash: Mapped[str] = mapped_column(String(72), nullable=False)




class InferenceBudgetAccountRow(Base):
    """Three counters per cost authority: global, tenant, task."""

    __tablename__ = "safe_inference_budget_accounts"
    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    limit_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    held_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class InferenceBudgetReservationRow(Base):
    """A submitted inference may be billed even if its outcome is unknown."""

    __tablename__ = "safe_inference_budget_reservations"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("safe_tasks.task_id"), nullable=False, index=True,
    )
    route_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_digest: Mapped[str] = mapped_column(String(72), nullable=False)
    backend_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    max_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    held_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_input_tokens: Mapped[int | None] = mapped_column(Integer)
    reported_output_tokens: Mapped[int | None] = mapped_column(Integer)
    evidence_hash: Mapped[str | None] = mapped_column(String(72))
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_event_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("uq_safe_inference_reservation_binding", "request_id", "organization_id",
              "task_id", "route_id", "model_revision", "prompt_digest", "deadline_at",
              unique=True),
    )


class InferenceBudgetEventRow(Base):
    """Append-only, hash-linked inference budget transitions. No raw prompts."""

    __tablename__ = "safe_inference_budget_events"
    request_id: Mapped[str] = mapped_column(
        ForeignKey("safe_inference_budget_reservations.request_id"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    held_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(String(72))
    reason_code: Mapped[str | None] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    preceding_hash: Mapped[str | None] = mapped_column(String(72))
    event_hash: Mapped[str] = mapped_column(String(72), nullable=False)


class InferenceBillingAttestationRow(Base):
    """Immutable externally asserted charge evidence, not a trusted invoice reader."""

    __tablename__ = "safe_inference_billing_attestations"
    receipt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("safe_inference_budget_reservations.request_id"),
        nullable=False, unique=True,
    )
    issuer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    charge_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_evidence_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    attestation_digest: Mapped[str] = mapped_column(String(72), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InferenceInvocationRow(Base):
    """M7-05a schema foundation only; runtime admission is not wired yet.

    Composite references bind one planning attempt to its exact cost reservation.
    Identity is immutable on migrated PostgreSQL; no prompt/output is retained.
    """

    __tablename__ = "safe_inference_invocations"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_digest: Mapped[str] = mapped_column(String(72), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(72), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_version: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposal_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_event_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ['task_id', 'organization_id', 'manifest_hash'],
            ['safe_tasks.task_id', 'safe_tasks.organization_id', 'safe_tasks.manifest_hash'],
            name='fk_infer_invocation_task',
        ),
        ForeignKeyConstraint(
            ['attempt_id', 'task_id', 'manifest_hash', 'task_state_version', 'lease_expires_at'],
            ['safe_planning_attempts.attempt_id', 'safe_planning_attempts.task_id', 'safe_planning_attempts.manifest_hash', 'safe_planning_attempts.task_state_version', 'safe_planning_attempts.lease_until'],
            name='fk_infer_invocation_attempt',
        ),
        ForeignKeyConstraint(
            ['request_id', 'organization_id', 'task_id', 'route_id', 'model_revision', 'prompt_digest', 'deadline_at'],
            ['safe_inference_budget_reservations.request_id', 'safe_inference_budget_reservations.organization_id', 'safe_inference_budget_reservations.task_id', 'safe_inference_budget_reservations.route_id', 'safe_inference_budget_reservations.model_revision', 'safe_inference_budget_reservations.prompt_digest', 'safe_inference_budget_reservations.deadline_at'],
            name='fk_infer_invocation_reservation',
        ),
        UniqueConstraint("organization_id", "task_id", "attempt_id",
                         name="uq_infer_invocation_attempt"),
        UniqueConstraint("request_id", "binding_hash", name="uq_infer_invocation_identity"),
        Index("ix_safe_inference_invocations_recovery", "state", "deadline_at"),
        CheckConstraint(
            "state IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED', "
            "'FAILED_BEFORE_DISPATCH', 'UNKNOWN')",
            name='ck_infer_invocation_state',
        ),
        CheckConstraint(
            'task_state_version >= 0 AND workspace_version >= 1 AND state_version >= 0 AND '
            'claim_version >= 0',
            name='ck_infer_invocation_versions',
        ),
        CheckConstraint(
            'event_sequence >= 1 AND state_version = event_sequence - 1',
            name='ck_infer_invocation_sequence',
        ),
        CheckConstraint(
            'issued_at < deadline_at AND deadline_at <= lease_expires_at AND updated_at >= '
            'issued_at',
            name='ck_infer_invocation_deadline',
        ),
        CheckConstraint(
            '((claim_owner IS NULL AND claim_until IS NULL) OR (claim_owner IS NOT NULL AND '
            'claim_until IS NOT NULL AND claim_version > 0 AND issued_at < claim_until AND '
            'claim_until <= deadline_at))',
            name='ck_infer_invocation_claim',
        ),
        CheckConstraint(
            "((state IN ('RESERVED', 'FAILED_BEFORE_DISPATCH') AND dispatch_started_at IS "
            "NULL) OR (state IN ('DISPATCH_STARTED', 'RESPONSE_VALIDATED', 'UNKNOWN') AND "
            'dispatch_started_at IS NOT NULL AND issued_at <= dispatch_started_at AND '
            'dispatch_started_at < deadline_at AND dispatch_started_at <= updated_at))',
            name='ck_infer_invocation_dispatch',
        ),
        CheckConstraint(
            "((state = 'RESPONSE_VALIDATED' AND proposal_hash IS NOT NULL) OR (state <> "
            "'RESPONSE_VALIDATED' AND proposal_hash IS NULL))",
            name='ck_infer_invocation_result',
        ),
        CheckConstraint(
            "((state = 'FAILED_BEFORE_DISPATCH' AND reason_code IS NOT NULL AND reason_code "
            "IN ('AUTHORITY_EXPIRED', 'CANCELLED_BEFORE_DISPATCH', "
            "'INFRASTRUCTURE_UNAVAILABLE')) OR (state = 'UNKNOWN' AND reason_code IS NOT NULL "
            "AND reason_code IN ('DISPATCH_OUTCOME_UNKNOWN', 'RESPONSE_REJECTED')) OR (state "
            "IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED') AND reason_code IS "
            'NULL))',
            name='ck_infer_invocation_reason',
        ),
    )


class InferenceInvocationEventRow(Base):
    """Append-only lifecycle metadata; billing and business effects stay separate."""

    __tablename__ = "safe_inference_invocation_events"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposal_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    preceding_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ['request_id', 'binding_hash'],
            ['safe_inference_invocations.request_id', 'safe_inference_invocations.binding_hash'],
            name='fk_infer_invocation_event_binding',
        ),
        CheckConstraint(
            'sequence >= 1',
            name='ck_infer_invocation_event_sequence',
        ),
        CheckConstraint(
            '((sequence = 1 AND previous_state IS NULL AND preceding_hash IS NULL AND state = '
            "'RESERVED') OR (sequence > 1 AND previous_state IS NOT NULL AND preceding_hash "
            'IS NOT NULL))',
            name='ck_infer_invocation_event_order',
        ),
        CheckConstraint(
            "((previous_state IS NULL AND state = 'RESERVED') OR (previous_state = 'RESERVED' "
            "AND state IN ('DISPATCH_STARTED', 'FAILED_BEFORE_DISPATCH')) OR (previous_state "
            "= 'DISPATCH_STARTED' AND state IN ('RESPONSE_VALIDATED', 'UNKNOWN')))",
            name='ck_infer_invocation_event_transition',
        ),
        CheckConstraint(
            "((state = 'RESPONSE_VALIDATED' AND proposal_hash IS NOT NULL) OR (state <> "
            "'RESPONSE_VALIDATED' AND proposal_hash IS NULL))",
            name='ck_infer_invocation_event_result',
        ),
        CheckConstraint(
            "((state = 'FAILED_BEFORE_DISPATCH' AND reason_code IS NOT NULL AND reason_code "
            "IN ('AUTHORITY_EXPIRED', 'CANCELLED_BEFORE_DISPATCH', "
            "'INFRASTRUCTURE_UNAVAILABLE')) OR (state = 'UNKNOWN' AND reason_code IS NOT NULL "
            "AND reason_code IN ('DISPATCH_OUTCOME_UNKNOWN', 'RESPONSE_REJECTED')) OR (state "
            "IN ('RESERVED', 'DISPATCH_STARTED', 'RESPONSE_VALIDATED') AND reason_code IS "
            'NULL))',
            name='ck_infer_invocation_event_reason',
        ),
    )


class ManifestRow(Base):
    __tablename__ = "safe_manifest_versions"
    manifest_hash: Mapped[str] = mapped_column(String(72), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "manifest_version", name="uq_safe_manifest_task_version"),)


class ProjectionRow(Base):
    __tablename__ = "safe_projections"
    revision: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, index=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    projection_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SimulationRow(Base):
    __tablename__ = "safe_simulations"
    simulation_ref: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, index=True)
    projection_rev: Mapped[str] = mapped_column(ForeignKey("safe_projections.revision"), nullable=False)
    simulation_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    simulation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProposalRow(Base):
    __tablename__ = "safe_proposals"
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), primary_key=True)
    cycle_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    simulation_ref: Mapped[str] = mapped_column(ForeignKey("safe_simulations.simulation_ref"), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "proposal_hash", name="uq_safe_proposal_hash"),)


class ApprovalRow(Base):
    __tablename__ = "safe_approvals"
    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, index=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    approval_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "proposal_hash", name="uq_safe_decision_per_proposal"),)


class ReferenceRow(Base):
    __tablename__ = "safe_protected_references"
    handle: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, index=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # TRUSTED ONLY
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ExecutionRow(Base):
    __tablename__ = "safe_execution_records"
    effect_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, index=True)
    proposal_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "proposal_hash", name="uq_safe_effect_per_proposal"),)


class LedgerRow(Base):
    __tablename__ = "safe_evidence_ledger"
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), primary_key=True)
    ledger_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    previous_event_hash: Mapped[str | None] = mapped_column(String(72))
    event_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("ix_safe_ledger_org_task", "organization_id", "task_id"),)


class OutboxRow(Base):
    """Trusted dispatch command: no raw customer/provider IDs in this table."""

    __tablename__ = "safe_action_outbox"
    action_hash: Mapped[str] = mapped_column(String(72), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("safe_tasks.task_id"), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    provider_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="synthetic", server_default="synthetic")
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reason: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_safe_outbox_ready", "status", "next_attempt_at"),)


class OfficeExecutionAuthorizationRow(Base):
    """Server-only TAL authorization, bound to one durable outbox action."""

    __tablename__ = "safe_office_execution_authorizations"
    action_hash: Mapped[str] = mapped_column(
        String(72), ForeignKey("safe_action_outbox.action_hash"), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_tasks.task_id"), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    authorization_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FakeProviderRow(Base):
    __tablename__ = "safe_fake_provider_state"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(72), nullable=False)


class FakeProviderEffectRow(Base):
    __tablename__ = "safe_fake_provider_effects"
    action_hash: Mapped[str] = mapped_column(String(72), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    effect_ref: Mapped[str] = mapped_column(String(128), nullable=False)


class GoogleTestOutboxBindingRow(Base):
    """Trusted-only Google test connection and observation tied to ONE SAFE outbox.

    The exact provider calendar ID and OAuth credentials remain exclusively in
    the encrypted GoogleTestConnectionRow vault. This is not a public DTO.
    """

    __tablename__ = "safe_google_test_outbox_bindings"
    action_hash: Mapped[str] = mapped_column(
        String(72), ForeignKey("safe_action_outbox.action_hash"), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_tasks.task_id"), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connection_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("safe_google_test_connections.connection_id"),
        nullable=False)
    calendar_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_revision: Mapped[str] = mapped_column(String(72), nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    provider_request_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    # Nullable for historical PR2 intents: they fail closed until re-staged.
    provider_simulation_hash: Mapped[str | None] = mapped_column(String(72))
    provider_simulation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoogleOAuthPendingRow(Base):
    """Short-lived state + encrypted PKCE verifier; never model-visible."""

    __tablename__ = "safe_google_oauth_pending"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    calendar_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoogleTestConnectionRow(Base):
    """Secret vault, test-only: no provider identifiers or tokens in model projection."""

    __tablename__ = "safe_google_test_connections"
    connection_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    refresh_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    calendar_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[str] = mapped_column(String(256), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
