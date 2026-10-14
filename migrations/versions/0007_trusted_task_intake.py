"""M6 trusted, idempotent task assignment backed by one atomic SQL transaction.

Revision ID: 0007_trusted_task_intake
Revises: 0006_google_provider_simulation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_trusted_task_intake"
down_revision = "0006_google_provider_simulation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_task_intake_keys",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("initiator_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(72), nullable=False),
        sa.Column(
            "task_id", sa.String(128),
            sa.ForeignKey("safe_tasks.task_id"), unique=True, nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER safe_task_intake_key_immutable BEFORE UPDATE OR DELETE "
            "ON safe_task_intake_keys FOR EACH ROW "
            "EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Immutable task idempotency history requires reviewed backup")
