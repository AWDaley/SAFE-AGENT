"""Trusted persistence mapping; NEVER serialize protected bindings to model context."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
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

    __table_args__ = (Index("ix_safe_tasks_org_actor", "organization_id", "initiator_id"),)


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
