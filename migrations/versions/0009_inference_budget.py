"""M7-03 SQL-backed inference spending limits and immutable usage evidence.

Revision ID: 0009_inference_budget
Revises: 0008_planning_workspace
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_inference_budget"
down_revision = "0008_planning_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_inference_budget_scopes",
        sa.Column("scope_key", sa.String(260), primary_key=True),
        sa.Column("profile_hash", sa.String(72), nullable=False),
        sa.Column("max_microusd", sa.BigInteger(), nullable=False),
        sa.Column("held_microusd", sa.BigInteger(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("calls_used", sa.Integer(), nullable=False),
        sa.Column("max_inflight", sa.Integer(), nullable=False),
        sa.Column("active_calls", sa.Integer(), nullable=False),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "safe_inference_budget_reservations",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128),
                  sa.ForeignKey("safe_tasks.task_id"), nullable=False),
        sa.Column("route_id", sa.String(128), nullable=False),
        sa.Column("model_revision", sa.String(128), nullable=False),
        sa.Column("prompt_digest", sa.String(72), nullable=False),
        sa.Column("request_hash", sa.String(72), nullable=False),
        sa.Column("profile_hash", sa.String(72), nullable=False),
        sa.Column("price_policy_hash", sa.String(72), nullable=False),
        sa.Column("input_price_microusd", sa.BigInteger(), nullable=False),
        sa.Column("output_price_microusd", sa.BigInteger(), nullable=False),
        sa.Column("input_proxy_bytes", sa.Integer(), nullable=False),
        sa.Column("output_proxy_bytes", sa.Integer(), nullable=False),
        sa.Column("maximum_held_microusd", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("uncertainty_reason", sa.String(40)),
        sa.Column("reported_input_tokens", sa.Integer()),
        sa.Column("reported_output_tokens", sa.Integer()),
        sa.Column("reported_source", sa.String(24)),
        sa.Column("reported_estimated_microusd", sa.BigInteger()),
        sa.Column("proposal_digest", sa.String(72)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_safe_inference_budget_reservations_organization_id",
        "safe_inference_budget_reservations", ["organization_id"],
    )
    op.create_index(
        "ix_safe_inference_budget_reservations_task_id",
        "safe_inference_budget_reservations", ["task_id"],
    )
    op.create_index(
        "ix_safe_inference_reservation_state_deadline",
        "safe_inference_budget_reservations", ["status", "deadline_at"],
    )
    op.create_table(
        "safe_inference_budget_events",
        sa.Column("request_id", sa.String(36),
                  sa.ForeignKey("safe_inference_budget_reservations.request_id"),
                  primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(72)),
        sa.Column("event_hash", sa.String(72), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER safe_inference_events_immutable "
            "BEFORE UPDATE OR DELETE ON safe_inference_budget_events "
            "FOR EACH ROW EXECUTE FUNCTION safe_reject_immutable_update()"
        )


def downgrade() -> None:
    raise RuntimeError("Inference budget and usage history cannot be silently deleted")
