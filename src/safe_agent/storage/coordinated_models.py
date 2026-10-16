"""W4 immutable input and consumption mappings, separate from cost accounting."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DDL, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from safe_agent.storage.models import Base


class CoordinatedPlanningInputRow(Base):
    __tablename__ = "safe_coordinated_planning_inputs"
    attempt_id: Mapped[str] = mapped_column(String(128), ForeignKey("safe_planning_attempts.attempt_id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), ForeignKey("safe_tasks.task_id"), unique=True, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    task_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("task_state_version >= 0 AND workspace_version >= 1", name="ck_coord_input_version"),
        CheckConstraint("length(record_json) BETWEEN 1 AND 65536", name="ck_coord_input_size"),
    )


class CoordinatedPlanningConsumptionRow(Base):
    __tablename__ = "safe_coordinated_planning_consumptions"
    attempt_id: Mapped[str] = mapped_column(String(128), ForeignKey("safe_coordinated_planning_inputs.attempt_id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), ForeignKey("safe_inference_validated_results.request_id"), unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), ForeignKey("safe_tasks.task_id"), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    result_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal_hash: Mapped[str | None] = mapped_column(String(72))
    outcome_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumption_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    __table_args__ = (
        CheckConstraint("(result_kind = 'PROPOSED' AND proposal_hash IS NOT NULL) OR "
                        "(result_kind = 'NEEDS_CLARIFICATION' AND proposal_hash IS NULL)",
                        name="ck_coord_consumption_kind"),
    )


INPUT_MATCH = """
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
CONSUMPTION_MATCH = """
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

# Isolated create_all fixtures get the same SQLite guards as the migration.
# PostgreSQL acceptance must use the real migration, never create_all as a substitute.
for table, prefix, match in (
    (CoordinatedPlanningInputRow.__table__, "coord_input", INPUT_MATCH),
    (CoordinatedPlanningConsumptionRow.__table__, "coord_consumption", CONSUMPTION_MATCH),
):
    for operation in ("UPDATE", "DELETE"):
        event.listen(table, "after_create", DDL(
            f"CREATE TRIGGER safe_{prefix}_no_{operation.lower()} BEFORE {operation} ON {table.name} "
            "BEGIN SELECT RAISE(ABORT, 'COORDINATED_EVIDENCE_IMMUTABLE'); END"
        ).execute_if(dialect="sqlite"))
    event.listen(table, "after_create", DDL(
        f"CREATE TRIGGER safe_{prefix}_insert BEFORE INSERT ON {table.name} "
        f"WHEN EXISTS (SELECT 1 FROM {table.name} WHERE attempt_id = NEW.attempt_id) "
        f"OR NOT EXISTS ({match}) BEGIN SELECT RAISE(ABORT, 'COORDINATED_EVIDENCE_DENIED'); END"
    ).execute_if(dialect="sqlite"))
