"""Trusted persistence mapping; NEVER serialize protected bindings to model context."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
