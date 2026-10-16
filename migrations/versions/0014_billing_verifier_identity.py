"""Billing verifier identity and key-rotation attribution.

Revision ID: 0014_billing_verifier_identity
Revises: 0013_coordinated_planning

Adds only immutable attribution metadata to existing billing attestations.
No reservation, charge, task or provider-effect state is rewritten.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_billing_verifier_identity"
down_revision = "0013_coordinated_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "safe_inference_billing_attestations"
    op.add_column(
        table,
        sa.Column(
            "verification_scheme", sa.String(24), nullable=False,
            server_default=sa.text("'legacy_hmac'"),
        ),
    )
    op.add_column(table, sa.Column("verifier_key_id", sa.String(128), nullable=True))
    op.add_column(table, sa.Column("verifier_public_key_hash", sa.String(72), nullable=True))
    op.add_column(
        table,
        sa.Column(
            "source_adapter_id", sa.String(128), nullable=False,
            server_default=sa.text("'legacy_hmac_v1'"),
        ),
    )

    dialect = op.get_bind().dialect.name
    identity = (
        "(verification_scheme = 'legacy_hmac' AND verifier_key_id IS NULL "
        "AND verifier_public_key_hash IS NULL) OR "
        "(verification_scheme = 'ed25519' AND verifier_key_id IS NOT NULL "
        "AND verifier_public_key_hash IS NOT NULL)"
    )
    if dialect == "postgresql":
        op.create_check_constraint(
            "ck_infer_billing_verification_identity", table, identity,
        )
    elif dialect == "sqlite":
        op.execute(f"""
            CREATE TRIGGER safe_inference_billing_identity_insert
            BEFORE INSERT ON {table}
            FOR EACH ROW WHEN NOT ({identity})
            BEGIN
                SELECT RAISE(ABORT, 'BILLING_VERIFICATION_IDENTITY_DENIED');
            END
        """)
        op.execute(f"""
            CREATE TRIGGER safe_inference_billing_identity_update
            BEFORE UPDATE OF verification_scheme, verifier_key_id,
                verifier_public_key_hash, source_adapter_id
            ON {table}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'BILLING_VERIFICATION_IDENTITY_IMMUTABLE');
            END
        """)
    else:
        raise RuntimeError("unsupported billing attribution database")


def downgrade() -> None:
    raise RuntimeError("Billing verifier identity history requires reviewed backup")
