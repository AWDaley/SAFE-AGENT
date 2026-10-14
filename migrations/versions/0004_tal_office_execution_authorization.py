"""Transactional TAL office authorization, no provider write path."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_tal_office_execution_authorization"
down_revision = "0003_google_test_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_office_execution_authorizations",
        sa.Column("action_hash", sa.String(72), sa.ForeignKey("safe_action_outbox.action_hash"), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False, unique=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("authorization_hash", sa.String(72), nullable=False),
        sa.Column("authorization_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER safe_office_authorizations_immutable BEFORE UPDATE OR DELETE "
            "ON safe_office_execution_authorizations FOR EACH ROW "
            "EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Authorization history is immutable; restore reviewed backup")
