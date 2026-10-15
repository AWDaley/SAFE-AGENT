"""M7-03 durable inference cost reservations and sealed billing evidence.

Revision ID: 0009_inference_cost_accounts
Revises: 0008_planning_workspace

All costs are integer micro-USD. The reservation does not prove an effect or
provider billing. M5 Google LIVE ACCEPTANCE remains BLOCKED.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_inference_cost_accounts"
down_revision = "0008_planning_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_inference_budget_accounts",
        sa.Column("scope_key", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("held_microusd", sa.BigInteger(), nullable=False),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("limit_microusd >= 0", name="ck_infer_account_limit"),
        sa.CheckConstraint("held_microusd >= 0", name="ck_infer_account_hold"),
        sa.CheckConstraint("charged_microusd >= 0", name="ck_infer_account_charge"),
        sa.CheckConstraint(
            "held_microusd + charged_microusd <= limit_microusd",
            name="ck_infer_account_conservation",
        ),
    )
    op.create_table(
        "safe_inference_budget_reservations",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column(
            "task_id", sa.String(128),
            sa.ForeignKey("safe_tasks.task_id"), nullable=False,
        ),
        sa.Column("route_id", sa.String(128), nullable=False),
        sa.Column("model_revision", sa.String(128), nullable=False),
        sa.Column("prompt_digest", sa.String(72), nullable=False),
        sa.Column("backend_kind", sa.String(12), nullable=False),
        sa.Column("max_microusd", sa.BigInteger(), nullable=False),
        sa.Column("held_microusd", sa.BigInteger(), nullable=False),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_input_tokens", sa.Integer()),
        sa.Column("reported_output_tokens", sa.Integer()),
        sa.Column("evidence_hash", sa.String(72)),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("latest_event_hash", sa.String(72), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_microusd >= 0", name="ck_infer_reserve_max"),
        sa.CheckConstraint("held_microusd >= 0", name="ck_infer_reserve_hold"),
        sa.CheckConstraint("charged_microusd >= 0", name="ck_infer_reserve_charge"),
        sa.CheckConstraint(
            "held_microusd + charged_microusd <= max_microusd",
            name="ck_infer_reserve_conservation",
        ),
    )
    op.create_index(
        "ix_safe_inference_budget_reservations_organization_id",
        "safe_inference_budget_reservations", ["organization_id"],
    )
    op.create_index(
        "ix_safe_inference_budget_reservations_task_id",
        "safe_inference_budget_reservations", ["task_id"],
    )
    op.create_table(
        "safe_inference_budget_events",
        sa.Column(
            "request_id", sa.String(128),
            sa.ForeignKey("safe_inference_budget_reservations.request_id"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event_kind", sa.String(24), nullable=False),
        sa.Column("held_microusd", sa.BigInteger(), nullable=False),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=False),
        sa.Column("evidence_hash", sa.String(72)),
        sa.Column("reason_code", sa.String(32)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preceding_hash", sa.String(72)),
        sa.Column("event_hash", sa.String(72), nullable=False),
    )
    op.create_table(
        "safe_inference_billing_attestations",
        sa.Column("receipt_id", sa.String(128), primary_key=True),
        sa.Column(
            "request_id", sa.String(128),
            sa.ForeignKey("safe_inference_budget_reservations.request_id"),
            nullable=False, unique=True,
        ),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column("charge_microusd", sa.BigInteger(), nullable=False),
        sa.Column("source_evidence_hash", sa.String(72), nullable=False),
        sa.Column("attestation_digest", sa.String(72), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "safe_inference_budget_events",
            "safe_inference_billing_attestations",
        ):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE "
                f"ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION safe_reject_immutable_update()"
            )


def downgrade() -> None:
    raise RuntimeError("Inference cost/attestation history requires reviewed backup")
