"""M2 task manifests, approvals, protected references, and immutable evidence.

Revision ID: 0001_office_ledger
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_office_ledger"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_tasks",
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("initiator_id", sa.String(128), nullable=False),
        sa.Column("manifest_id", sa.String(128), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("task_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("ledger_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_safe_tasks_organization_id", "safe_tasks", ["organization_id"])
    op.create_index("ix_safe_tasks_org_actor", "safe_tasks", ["organization_id", "initiator_id"])
    op.create_table(
        "safe_manifest_versions",
        sa.Column("manifest_hash", sa.String(72), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("manifest_id", sa.String(128), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "manifest_version", name="uq_safe_manifest_task_version"),
    )
    op.create_table(
        "safe_projections",
        sa.Column("revision", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("projection_hash", sa.String(72), nullable=False),
        sa.Column("projection_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_safe_projections_task_id", "safe_projections", ["task_id"])
    op.create_table(
        "safe_simulations",
        sa.Column("simulation_ref", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("projection_rev", sa.String(128), sa.ForeignKey("safe_projections.revision"), nullable=False),
        sa.Column("simulation_hash", sa.String(72), nullable=False),
        sa.Column("simulation_json", sa.JSON(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_safe_simulations_task_id", "safe_simulations", ["task_id"])
    op.create_table(
        "safe_proposals",
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), primary_key=True),
        sa.Column("cycle_number", sa.Integer(), primary_key=True),
        sa.Column("proposal_hash", sa.String(72), nullable=False),
        sa.Column("simulation_ref", sa.String(128), sa.ForeignKey("safe_simulations.simulation_ref"), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "proposal_hash", name="uq_safe_proposal_hash"),
    )
    op.create_table(
        "safe_approvals",
        sa.Column("approval_id", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("approver_id", sa.String(128), nullable=False),
        sa.Column("proposal_hash", sa.String(72), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("approval_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "proposal_hash", name="uq_safe_decision_per_proposal"),
    )
    op.create_index("ix_safe_approvals_task_id", "safe_approvals", ["task_id"])
    op.create_table(
        "safe_protected_references",
        sa.Column("handle", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("initiator_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("binding_json", sa.JSON(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_safe_protected_references_task_id", "safe_protected_references", ["task_id"])
    op.create_table(
        "safe_execution_records",
        sa.Column("effect_id", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("proposal_hash", sa.String(72), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "proposal_hash", name="uq_safe_effect_per_proposal"),
    )
    op.create_index("ix_safe_execution_records_task_id", "safe_execution_records", ["task_id"])
    op.create_table(
        "safe_evidence_ledger",
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), primary_key=True),
        sa.Column("ledger_sequence", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("event_hash", sa.String(72), nullable=False),
        sa.Column("previous_event_hash", sa.String(72)),
        sa.Column("event_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_safe_ledger_org_task", "safe_evidence_ledger", ["organization_id", "task_id"])
    if op.get_bind().dialect.name == "postgresql":
        # DB-role policy must also forbid the app user from altering this trigger.
        op.execute("""
        CREATE FUNCTION safe_reject_immutable_update() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
           RAISE EXCEPTION 'immutable SAFE-AGENT record cannot be updated or deleted';
        END; $$
        """)
        for table in ("safe_evidence_ledger", "safe_manifest_versions", "safe_approvals"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION safe_reject_immutable_update()")


def downgrade() -> None:
    # Deliberately refuse automatic deletion of audit trails on a live database.
    raise RuntimeError("M2 ledger migration is irreversible; restore from reviewed backup")
