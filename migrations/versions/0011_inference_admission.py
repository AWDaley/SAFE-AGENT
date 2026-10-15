"""M7-05b atomic admission metadata; unchanged 0009/0010 predecessors.

No dispatcher, account reset or provider access is introduced.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_inference_admission"
down_revision = "0010_inference_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_inference_admission_authority",
        sa.Column("authority_key", sa.String(16), primary_key=True),
        sa.Column("policy_hash", sa.String(72), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("authority_key", "policy_hash", name="uq_infer_admission_authority"),
        sa.CheckConstraint("authority_key = 'global'", name="ck_infer_admission_singleton"),
        sa.CheckConstraint("last_seen_at >= created_at", name="ck_infer_admission_clock"),
    )
    op.create_table(
        "safe_inference_admissions",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("binding_hash", sa.String(72), nullable=False),
        sa.Column("authority_key", sa.String(16), nullable=False),
        sa.Column("policy_hash", sa.String(72), nullable=False),
        sa.Column("intent_hash", sa.String(72), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id", "binding_hash"],
            ["safe_inference_invocations.request_id", "safe_inference_invocations.binding_hash"],
            name="fk_infer_admission_invocation",
        ),
        sa.ForeignKeyConstraint(
            ["authority_key", "policy_hash"],
            ["safe_inference_admission_authority.authority_key",
             "safe_inference_admission_authority.policy_hash"],
            name="fk_infer_admission_authority",
        ),
    )
    op.create_index("ix_safe_inference_admissions_window",
                    "safe_inference_admissions", ["admitted_at"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION safe_guard_inference_admission_authority() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'ADMISSION_AUTHORITY_IMMUTABLE';
                END IF;
                IF ROW(NEW.authority_key, NEW.policy_hash, NEW.created_at)
                   IS DISTINCT FROM ROW(OLD.authority_key, OLD.policy_hash, OLD.created_at)
                   OR NEW.last_seen_at < OLD.last_seen_at THEN
                    RAISE EXCEPTION 'ADMISSION_AUTHORITY_IMMUTABLE';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute(
            "CREATE TRIGGER safe_inference_admission_authority_guard BEFORE UPDATE OR DELETE "
            "ON safe_inference_admission_authority FOR EACH ROW "
            "EXECUTE FUNCTION safe_guard_inference_admission_authority()"
        )
        op.execute(
            "CREATE TRIGGER safe_inference_admissions_immutable BEFORE UPDATE OR DELETE "
            "ON safe_inference_admissions FOR EACH ROW "
            "EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Admission history cannot be reset; use reviewed backup recovery")
