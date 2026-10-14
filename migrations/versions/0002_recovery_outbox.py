"""M4 durable outbox, synthetic provider state and idempotency registry.

Revision ID: 0002_recovery_outbox
Revises: 0001_office_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_recovery_outbox"
down_revision = "0001_office_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_action_outbox",
        sa.Column("action_hash", sa.String(72), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False, unique=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("proposal_hash", sa.String(72), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("operation", sa.String(48), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("source_hash", sa.String(72), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reason", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_safe_outbox_ready", "safe_action_outbox", ["status", "next_attempt_at"])
    op.create_table(
        "safe_fake_provider_state",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("state_hash", sa.String(72), nullable=False),
    )
    op.create_table(
        "safe_fake_provider_effects",
        sa.Column("action_hash", sa.String(72), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(48), nullable=False),
        sa.Column("request_hash", sa.String(72), nullable=False),
        sa.Column("effect_ref", sa.String(128), nullable=False),
    )
    op.create_index("ix_safe_fake_provider_effects_organization_id", "safe_fake_provider_effects", ["organization_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE TRIGGER safe_fake_provider_effects_immutable BEFORE UPDATE OR DELETE "
                   "ON safe_fake_provider_effects FOR EACH ROW "
                   "EXECUTE FUNCTION safe_reject_immutable_update()")


def downgrade() -> None:
    raise RuntimeError("M4 recovery/idempotency history is irreversible; restore reviewed backup")
