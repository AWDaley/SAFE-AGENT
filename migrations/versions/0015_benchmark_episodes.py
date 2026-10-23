"""Add durable multi-turn benchmark episodes without rewriting office history.

Revision ID: 0015_benchmark_episodes
Revises: 0014_billing_verifier_identity

Frozen DDL intentionally does not import evolving runtime model definitions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_benchmark_episodes"
down_revision = "0014_billing_verifier_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_episode_runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("spec_hash", sa.String(71), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("turns", sa.BigInteger(), nullable=False),
        sa.Column("model_calls", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", sa.BigInteger(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "state_version >= 0 AND turns >= 0 AND model_calls >= 0 "
            "AND tool_calls >= 0 AND tokens >= 0 AND cost_microusd >= 0",
            name="ck_episode_run_budget",
        ),
    )
    op.create_table(
        "safe_episodes",
        sa.Column("episode_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("safe_episode_runs.run_id"),
                  nullable=False),
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column("spec_hash", sa.String(71), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.CheckConstraint("state_version >= 0", name="ck_episode_version"),
    )
    op.create_table(
        "safe_episode_events",
        sa.Column("episode_id", sa.String(128), sa.ForeignKey("safe_episodes.episode_id"),
                  primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128)),
        sa.Column("event_hash", sa.String(71), nullable=False, unique=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_episode_event_sequence"),
        sa.UniqueConstraint("episode_id", "kind", "external_id",
                            name="uq_episode_event_identity"),
    )
    op.create_table(
        "safe_episode_inference_reservations",
        sa.Column("episode_id", sa.String(128), sa.ForeignKey("safe_episodes.episode_id"),
                  primary_key=True),
        sa.Column("request_id", sa.String(128), primary_key=True, unique=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(71), nullable=False),
    )
    op.create_table(
        "safe_episode_inference_results",
        sa.Column("episode_id", sa.String(128), primary_key=True),
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("result_hash", sa.String(71), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id", "request_id"],
            ["safe_episode_inference_reservations.episode_id",
             "safe_episode_inference_reservations.request_id"],
        ),
    )
    op.create_table(
        "safe_episode_outgoing_calls",
        sa.Column("episode_id", sa.String(128), sa.ForeignKey("safe_episodes.episode_id"),
                  primary_key=True),
        sa.Column("call_id", sa.String(128), primary_key=True),
        sa.Column("action_hash", sa.String(71), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(71), nullable=False),
    )
    op.create_table(
        "safe_episode_tool_results",
        sa.Column("episode_id", sa.String(128), primary_key=True),
        sa.Column("call_id", sa.String(128), primary_key=True),
        sa.Column("result_hash", sa.String(71), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id", "call_id"],
            ["safe_episode_outgoing_calls.episode_id", "safe_episode_outgoing_calls.call_id"],
        ),
    )
    immutable_tables = (
        "safe_episode_events", "safe_episode_inference_reservations",
        "safe_episode_inference_results", "safe_episode_outgoing_calls",
        "safe_episode_tool_results",
    )
    run_update_denied = (
        "NEW.run_id != OLD.run_id OR NEW.spec_json != OLD.spec_json "
        "OR NEW.spec_hash != OLD.spec_hash OR NEW.state_version != OLD.state_version + 1 "
        "OR NEW.turns < OLD.turns OR NEW.model_calls < OLD.model_calls "
        "OR NEW.tool_calls < OLD.tool_calls OR NEW.tokens < OLD.tokens "
        "OR NEW.cost_microusd < OLD.cost_microusd"
    )
    episode_update_denied = (
        "NEW.episode_id != OLD.episode_id OR NEW.run_id != OLD.run_id "
        "OR NEW.spec_json != OLD.spec_json OR NEW.spec_hash != OLD.spec_hash "
        "OR NEW.state_version != OLD.state_version + 1"
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in immutable_tables:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'EPISODE_EVIDENCE_IMMUTABLE'); END"
                )
        for table, guard in (
            ("safe_episode_runs", run_update_denied),
            ("safe_episodes", episode_update_denied),
        ):
            op.execute(
                f"CREATE TRIGGER {table}_update_guard BEFORE UPDATE ON {table} "
                f"WHEN {guard} BEGIN "
                "SELECT RAISE(ABORT, 'EPISODE_IDENTITY_OR_BUDGET_DENIED'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'EPISODE_HISTORY_IMMUTABLE'); END"
            )
    elif dialect == "postgresql":
        op.execute("""
            CREATE FUNCTION safe_episode_deny_evidence_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'EPISODE_EVIDENCE_IMMUTABLE';
            END;
            $$ LANGUAGE plpgsql
        """)
        for table in immutable_tables:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION safe_episode_deny_evidence_mutation()"
            )
        for table, guard in (
            ("safe_episode_runs", run_update_denied),
            ("safe_episodes", episode_update_denied),
        ):
            op.execute(f"""
                CREATE FUNCTION {table}_guard_mutation() RETURNS trigger AS $$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'EPISODE_HISTORY_IMMUTABLE';
                    END IF;
                    IF {guard} THEN
                        RAISE EXCEPTION 'EPISODE_IDENTITY_OR_BUDGET_DENIED';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """)
            op.execute(
                f"CREATE TRIGGER {table}_mutation_guard BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {table}_guard_mutation()"
            )
    else:
        raise RuntimeError("unsupported episode database")


def downgrade() -> None:
    raise RuntimeError("Episode history and budget reservations require reviewed recovery")
