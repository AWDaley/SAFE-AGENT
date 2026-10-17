"""W4 immutable planning input and result consumption. No live-provider enablement.

Revision ID: 0013_coordinated_planning
Revises: 0012_inference_results
Frozen forward migration: no imports from evolving runtime mappings.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_coordinated_planning"
down_revision = "0012_inference_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_coordinated_planning_inputs",
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("safe_planning_attempts.attempt_id"), primary_key=True),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), unique=True, nullable=False),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("manifest_hash", sa.String(72), nullable=False),
        sa.Column("task_state_version", sa.Integer(), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("policy_hash", sa.String(72), nullable=False),
        sa.Column("input_hash", sa.String(72), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("task_state_version >= 0 AND workspace_version >= 1", name="ck_coord_input_version"),
        sa.CheckConstraint("length(record_json) BETWEEN 1 AND 65536", name="ck_coord_input_size"),
    )
    op.create_table(
        "safe_coordinated_planning_consumptions",
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("safe_coordinated_planning_inputs.attempt_id"), primary_key=True),
        sa.Column("request_id", sa.String(128), sa.ForeignKey("safe_inference_validated_results.request_id"), unique=True, nullable=False),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("input_hash", sa.String(72), nullable=False),
        sa.Column("result_hash", sa.String(72), nullable=False),
        sa.Column("candidate_hash", sa.String(72), nullable=False),
        sa.Column("result_kind", sa.String(32), nullable=False),
        sa.Column("proposal_hash", sa.String(72)),
        sa.Column("outcome_hash", sa.String(72), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumption_hash", sa.String(72), nullable=False),
        sa.CheckConstraint("(result_kind = 'PROPOSED' AND proposal_hash IS NOT NULL) OR "
                           "(result_kind = 'NEEDS_CLARIFICATION' AND proposal_hash IS NULL)",
                           name="ck_coord_consumption_kind"),
    )
    input_match = """
SELECT 1 FROM safe_planning_attempts a
JOIN safe_tasks t ON t.task_id = a.task_id
JOIN safe_planning_workspaces w ON w.task_id = a.task_id
WHERE a.attempt_id = NEW.attempt_id AND a.task_id = NEW.task_id
  AND t.organization_id = NEW.organization_id AND t.manifest_hash = NEW.manifest_hash
  AND a.manifest_hash = NEW.manifest_hash AND a.mode = 'text_only'
  AND a.task_state_version = NEW.task_state_version AND t.state_version = NEW.task_state_version
  AND w.active_attempt_id = a.attempt_id AND w.workspace_version = NEW.workspace_version
  AND w.status = 'RUNNING' AND w.mode = 'text_only'
  AND a.started_at <= NEW.recorded_at AND NEW.recorded_at < a.lease_until
"""
    consumption_match = """
SELECT 1 FROM safe_coordinated_planning_inputs p
JOIN safe_inference_validated_results r ON r.attempt_id = p.attempt_id
JOIN safe_planning_outcomes o ON o.attempt_id = p.attempt_id
JOIN safe_planning_workspaces w ON w.task_id = p.task_id
WHERE p.attempt_id = NEW.attempt_id AND p.task_id = NEW.task_id
  AND p.input_hash = NEW.input_hash AND r.request_id = NEW.request_id
  AND r.task_id = NEW.task_id AND r.result_hash = NEW.result_hash
  AND r.candidate_hash = NEW.candidate_hash AND p.policy_hash = r.policy_hash
  AND o.outcome_hash = NEW.outcome_hash AND o.result_kind = NEW.result_kind
  AND o.recorded_at = NEW.recorded_at AND r.validated_at <= NEW.recorded_at
  AND ((NEW.result_kind = 'PROPOSED' AND o.proposal_hash = NEW.proposal_hash
        AND w.status = 'PREPARED' AND w.proposal_hash = NEW.proposal_hash
        AND EXISTS (SELECT 1 FROM safe_proposals a WHERE a.task_id = NEW.task_id
                    AND a.proposal_hash = NEW.proposal_hash))
       OR (NEW.result_kind = 'NEEDS_CLARIFICATION' AND o.proposal_hash IS NULL
           AND w.status IN ('NEEDS_CLARIFICATION', 'EXHAUSTED')))
"""
    dialect = op.get_bind().dialect.name
    for table, prefix, match in (
        ("safe_coordinated_planning_inputs", "coord_input", input_match),
        ("safe_coordinated_planning_consumptions", "coord_consumption", consumption_match),
    ):
        if dialect == "sqlite":
            for operation in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER safe_{prefix}_no_{operation.lower()} BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'COORDINATED_EVIDENCE_IMMUTABLE'); END")
            op.execute(f"CREATE TRIGGER safe_{prefix}_insert BEFORE INSERT ON {table} "
                       f"WHEN EXISTS (SELECT 1 FROM {table} WHERE attempt_id = NEW.attempt_id) "
                       f"OR NOT EXISTS ({match}) BEGIN SELECT RAISE(ABORT, 'COORDINATED_EVIDENCE_DENIED'); END")
        elif dialect == "postgresql":
            op.execute(f"""
                CREATE FUNCTION safe_guard_{prefix}() RETURNS trigger AS $$
                BEGIN
                    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'COORDINATED_EVIDENCE_IMMUTABLE'; END IF;
                    IF NOT EXISTS ({match}) THEN RAISE EXCEPTION 'COORDINATED_EVIDENCE_DENIED'; END IF;
                    RETURN NEW;
                END; $$ LANGUAGE plpgsql
            """)
            op.execute(f"CREATE TRIGGER safe_{prefix}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                       f"FOR EACH ROW EXECUTE FUNCTION safe_guard_{prefix}()")
        else:
            raise RuntimeError("unsupported coordinated evidence database")


def downgrade() -> None:
    raise RuntimeError("Coordinated planning history cannot be reset; use reviewed recovery")
