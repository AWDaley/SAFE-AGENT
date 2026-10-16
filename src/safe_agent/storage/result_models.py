"""W3 additive result mapping and create_all guards for isolated test databases.

The frozen Alembic migration repeats this schema/DDL independently. Existing
invocation and budget mappings and historical migrations are not modified.
"""
from datetime import datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from safe_agent.storage.models import Base


class InferenceValidatedResultRow(Base):
    __tablename__ = "safe_inference_validated_results"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    validation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_event_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["request_id"], ["safe_inference_admissions.request_id"],
                             name="fk_infer_result_admission"),
        ForeignKeyConstraint(["request_id", "binding_hash"],
                             ["safe_inference_invocations.request_id",
                              "safe_inference_invocations.binding_hash"],
                             name="fk_infer_result_binding"),
        ForeignKeyConstraint(["request_id", "validation_sequence"],
                             ["safe_inference_invocation_events.request_id",
                              "safe_inference_invocation_events.sequence"],
                             name="fk_infer_result_event"),
        UniqueConstraint("organization_id", "task_id", "attempt_id",
                         name="uq_infer_result_attempt"),
        CheckConstraint("validation_sequence = 3", name="ck_infer_result_sequence"),
        CheckConstraint("length(record_json) BETWEEN 1 AND 65536", name="ck_infer_result_size"),
    )


# Match the committed validation identity, including policy and tenant, even for
# direct SQL inserts. Digests in JSON are additionally verified by trusted reads.
_MATCH = """
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
_table = InferenceValidatedResultRow.__table__
for operation in ("UPDATE", "DELETE"):
    event.listen(_table, "after_create", DDL(
        f"CREATE TRIGGER safe_result_no_{operation.lower()} BEFORE {operation} "
        "ON safe_inference_validated_results BEGIN "
        "SELECT RAISE(ABORT, 'INFERENCE_RESULT_IMMUTABLE'); END"
    ).execute_if(dialect="sqlite"))
event.listen(_table, "after_create", DDL(
    "CREATE TRIGGER safe_result_insert_guard BEFORE INSERT ON safe_inference_validated_results "
    "WHEN EXISTS (SELECT 1 FROM safe_inference_validated_results WHERE request_id = NEW.request_id) "
    f"OR NOT EXISTS ({_MATCH}) BEGIN SELECT RAISE(ABORT, 'INFERENCE_RESULT_INSERT_DENIED'); END"
).execute_if(dialect="sqlite"))
event.listen(_table, "after_create", DDL(f"""
    CREATE FUNCTION safe_guard_inference_result() RETURNS trigger AS $$
    BEGIN
        IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'INFERENCE_RESULT_IMMUTABLE'; END IF;
        IF NOT EXISTS ({_MATCH}) THEN RAISE EXCEPTION 'INFERENCE_RESULT_INSERT_DENIED'; END IF;
        RETURN NEW;
    END; $$ LANGUAGE plpgsql
""").execute_if(dialect="postgresql"))
event.listen(_table, "after_create", DDL(
    "CREATE TRIGGER safe_inference_result_guard BEFORE INSERT OR UPDATE OR DELETE "
    "ON safe_inference_validated_results FOR EACH ROW EXECUTE FUNCTION safe_guard_inference_result()"
).execute_if(dialect="postgresql"))
