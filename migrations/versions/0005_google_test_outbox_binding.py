"""M5 PR2: distinguish synthetic and Google test outbox commands.

Revision ID: 0005_google_test_outbox_binding
Revises: 0004_tal_office_execution_authorization
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_google_test_outbox_binding"
down_revision = "0004_tal_office_execution_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("safe_action_outbox", sa.Column(
        "provider_kind", sa.String(32), nullable=False, server_default="synthetic"))
    op.create_index("ix_safe_outbox_provider_ready", "safe_action_outbox",
                    ["provider_kind", "status", "next_attempt_at"])
    op.create_table(
        "safe_google_test_outbox_bindings",
        sa.Column("action_hash", sa.String(72),
                  sa.ForeignKey("safe_action_outbox.action_hash"), primary_key=True),
        sa.Column("task_id", sa.String(128),
                  sa.ForeignKey("safe_tasks.task_id"), nullable=False, unique=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("initiator_id", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.String(128),
                  sa.ForeignKey("safe_google_test_connections.connection_id"), nullable=False),
        sa.Column("calendar_ref", sa.String(128), nullable=False),
        sa.Column("observation_revision", sa.String(72), nullable=False),
        sa.Column("observation_hash", sa.String(72), nullable=False),
        sa.Column("provider_request_hash", sa.String(72), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER safe_google_test_outbox_binding_immutable BEFORE UPDATE OR DELETE "
            "ON safe_google_test_outbox_bindings FOR EACH ROW "
            "EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Immutable Google outbox history requires a reviewed backup")
