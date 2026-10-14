"""PR4 provider-sourced advisory simulation bound to Google test outbox.

Revision ID: 0006_google_provider_simulation
Revises: 0005_google_test_outbox_binding

Existing staged PR2 rows have no provider simulation: future workers must
reject them until replaced by an authorized, provider-backed staged action.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_google_provider_simulation"
down_revision = "0005_google_test_outbox_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "safe_google_test_outbox_bindings",
        sa.Column("provider_simulation_hash", sa.String(72), nullable=True),
    )
    op.add_column(
        "safe_google_test_outbox_bindings",
        sa.Column("provider_simulation_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("Historical provider simulation evidence must not be removed")
