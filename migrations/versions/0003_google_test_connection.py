"""Encrypted OAuth connection vault for isolated test calendars.

Revision ID: 0003_google_test_connection
Revises: 0002_recovery_outbox
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_google_test_connection"
down_revision = "0002_recovery_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_google_oauth_pending",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("calendar_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "safe_google_test_connections",
        sa.Column("connection_id", sa.String(128), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("refresh_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("calendar_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.String(256), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_safe_google_test_connections_organization_id",
                    "safe_google_test_connections", ["organization_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a reviewed backup before removing encrypted integration history")
