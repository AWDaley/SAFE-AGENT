"""M6-01e trusted task-scoped planning workspaces and immutable attempt evidence.

Revision ID: 0008_planning_workspace
Revises: 0007_trusted_task_intake
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_planning_workspace"
down_revision = "0007_trusted_task_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_planning_workspaces",
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"),
                  primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("initiator_id", sa.String(128), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("attempts_used", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_attempt_id", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_reason", sa.String(64)),
        sa.Column("proposal_hash", sa.String(72)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "safe_planning_attempts",
        sa.Column("attempt_id", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"),
                  nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("task_state_version", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "attempt_number",
                            name="uq_safe_planning_attempt_order"),
    )
    op.create_index("ix_safe_planning_attempts_task_id",
                    "safe_planning_attempts", ["task_id"])
    op.create_table(
        "safe_planning_outcomes",
        sa.Column("attempt_id", sa.String(128),
                  sa.ForeignKey("safe_planning_attempts.attempt_id"),
                  primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"),
                  nullable=False),
        sa.Column("result_kind", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("proposal_hash", sa.String(72)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome_hash", sa.String(72), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in ("safe_planning_attempts", "safe_planning_outcomes"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE "
                f"ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION safe_reject_immutable_update()"
            )


def downgrade() -> None:
    raise RuntimeError("Planning attempt evidence cannot be silently deleted")
