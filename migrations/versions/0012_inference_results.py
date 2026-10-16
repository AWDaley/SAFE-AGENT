"""W3 immutable validated results; forward-only, no backfill of missing output.

Revision ID: 0012_inference_results
Revises: 0011_inference_admission
This frozen migration never imports evolving runtime schema definitions.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_inference_results"
down_revision = "0011_inference_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_inference_validated_results",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("binding_hash", sa.String(72), nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("attempt_id", sa.String(128), nullable=False),
        sa.Column("policy_hash", sa.String(72), nullable=False),
        sa.Column("candidate_hash", sa.String(72), nullable=False),
        sa.Column("validation_sequence", sa.Integer(), nullable=False),
        sa.Column("validation_event_hash", sa.String(72), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_hash", sa.String(72), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["safe_inference_admissions.request_id"],
                                name="fk_infer_result_admission"),
        sa.ForeignKeyConstraint(["request_id", "binding_hash"],
                                ["safe_inference_invocations.request_id",
                                 "safe_inference_invocations.binding_hash"],
                                name="fk_infer_result_binding"),
        sa.ForeignKeyConstraint(["request_id", "validation_sequence"],
                                ["safe_inference_invocation_events.request_id",
                                 "safe_inference_invocation_events.sequence"],
                                name="fk_infer_result_event"),
        sa.UniqueConstraint("organization_id", "task_id", "attempt_id",
                            name="uq_infer_result_attempt"),
        sa.CheckConstraint("validation_sequence = 3", name="ck_infer_result_sequence"),
        sa.CheckConstraint("length(record_json) BETWEEN 1 AND 65536", name="ck_infer_result_size"),
    )
    match = """
        SELECT 1 FROM safe_inference_invocations i
        JOIN safe_inference_admissions a ON a.request_id = i.request_id
        JOIN safe_inference_invocation_events e ON e.request_id = i.request_id
        WHERE i.request_id = NEW.request_id AND i.binding_hash = NEW.binding_hash
          AND i.organization_id = NEW.organization_id AND i.task_id = NEW.task_id
          AND i.attempt_id = NEW.attempt_id AND i.state = 'RESPONSE_VALIDATED'
          AND i.proposal_hash = NEW.candidate_hash AND i.event_sequence = NEW.validation_sequence
          AND i.latest_event_hash = NEW.validation_event_hash AND a.policy_hash = NEW.policy_hash
          AND e.sequence = NEW.validation_sequence AND e.state = 'RESPONSE_VALIDATED'
          AND e.event_hash = NEW.validation_event_hash AND e.proposal_hash = NEW.candidate_hash
          AND e.occurred_at = NEW.validated_at
    """
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER safe_result_no_{operation.lower()} BEFORE {operation} "
                "ON safe_inference_validated_results BEGIN "
                "SELECT RAISE(ABORT, 'INFERENCE_RESULT_IMMUTABLE'); END"
            )
        op.execute(
            "CREATE TRIGGER safe_result_insert_guard BEFORE INSERT ON safe_inference_validated_results "
            "WHEN EXISTS (SELECT 1 FROM safe_inference_validated_results WHERE request_id = NEW.request_id) "
            f"OR NOT EXISTS ({match}) BEGIN SELECT RAISE(ABORT, 'INFERENCE_RESULT_INSERT_DENIED'); END"
        )
    elif dialect == "postgresql":
        op.execute(f"""
            CREATE FUNCTION safe_guard_inference_result() RETURNS trigger AS $$
            BEGIN
                IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'INFERENCE_RESULT_IMMUTABLE'; END IF;
                IF NOT EXISTS ({match}) THEN RAISE EXCEPTION 'INFERENCE_RESULT_INSERT_DENIED'; END IF;
                RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        op.execute(
            "CREATE TRIGGER safe_inference_result_guard BEFORE INSERT OR UPDATE OR DELETE "
            "ON safe_inference_validated_results FOR EACH ROW EXECUTE FUNCTION safe_guard_inference_result()"
        )
    else:
        raise RuntimeError("unsupported result evidence database")


def downgrade() -> None:
    raise RuntimeError("Validated inference history cannot be reset; use reviewed recovery")
