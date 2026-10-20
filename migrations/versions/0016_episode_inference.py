"""Private episode context and fenced model results; additive to office records.

Frozen forward DDL; does not import mutable runtime mapping definitions.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_episode_inference"
down_revision = "0015_benchmark_episodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_episode_private_contexts",
        sa.Column("episode_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("context_hash", sa.String(72), nullable=False),
        sa.Column("active_observation", sa.String(128), nullable=True),
        sa.CheckConstraint("version >= 0", name="ck_episode_context_version"),
    )
    op.create_table(
        "safe_episode_model_attempts",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("episode_id", sa.String(128), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(72), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reply_json", sa.Text(), nullable=True),
        sa.Column("reply_hash", sa.String(72), nullable=True),
        sa.CheckConstraint("status IN ('PREPARED', 'DISPATCHING', 'COMPLETE', 'UNCERTAIN')",
                           name="ck_episode_model_status"),
        sa.CheckConstraint(
            "(status = 'COMPLETE' AND reply_json IS NOT NULL AND reply_hash IS NOT NULL) OR "
            "(status != 'COMPLETE' AND reply_json IS NULL AND reply_hash IS NULL)",
            name="ck_episode_model_reply_state",
        ),
    )
    op.create_index("ix_safe_episode_model_attempts_episode_id",
                    "safe_episode_model_attempts", ["episode_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        guards = (
            ("safe_episode_model_attempts", "episode_model", """
                NEW.status != 'PREPARED' OR NEW.reply_json IS NOT NULL OR NEW.reply_hash IS NOT NULL
                OR CASE WHEN json_valid(NEW.request_json) THEN
                    json_type(NEW.request_json) != 'object'
                    OR json_extract(NEW.request_json, '$.request_id') IS NOT NEW.request_id
                    OR json_extract(NEW.request_json, '$.episode_id') IS NOT NEW.episode_id
                    OR json_extract(NEW.request_json, '$.model') IS NULL
                ELSE 1 END
            """, """
                NEW.request_id != OLD.request_id OR NEW.episode_id != OLD.episode_id
                OR NEW.request_json != OLD.request_json OR NEW.input_hash != OLD.input_hash
                OR NOT (
                    (OLD.status = 'PREPARED' AND NEW.status = 'DISPATCHING')
                    OR (OLD.status = 'DISPATCHING' AND NEW.status IN ('COMPLETE', 'UNCERTAIN'))
                )
                OR (NEW.status = 'COMPLETE' AND CASE WHEN json_valid(NEW.reply_json) THEN
                    json_type(NEW.reply_json) != 'object'
                    OR json_extract(NEW.reply_json, '$.request_id') IS NOT NEW.request_id
                    OR json_extract(NEW.reply_json, '$.input_hash') IS NOT NEW.input_hash
                    OR json_extract(NEW.reply_json, '$.model') IS NOT json_extract(NEW.request_json, '$.model')
                ELSE 1 END)
            """),
            ("safe_episode_private_contexts", "episode_context", """
                NEW.version != 0 OR NEW.active_observation IS NOT NULL
            """, """
                NEW.episode_id != OLD.episode_id OR NEW.version != OLD.version + 1
                OR NOT (
                    (OLD.active_observation IS NULL AND NEW.active_observation IS NOT NULL
                     AND NEW.context_json = OLD.context_json AND NEW.context_hash = OLD.context_hash)
                    OR (OLD.active_observation IS NOT NULL AND NEW.active_observation IS NULL)
                )
            """),
        )
        for table, prefix, insert_guard, update_guard in guards:
            for operation, guard in (("INSERT", insert_guard), ("UPDATE", update_guard)):
                op.execute(
                    f"CREATE TRIGGER safe_{prefix}_{operation.lower()}_guard "
                    f"BEFORE {operation} ON {table} WHEN {guard} "
                    "BEGIN SELECT RAISE(ABORT, 'EPISODE_RUNTIME_FENCE_DENIED'); END"
                )
            op.execute(
                f"CREATE TRIGGER safe_{prefix}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'EPISODE_RUNTIME_HISTORY_IMMUTABLE'); END"
            )
    elif dialect == "postgresql":
        op.execute("""
            CREATE FUNCTION safe_guard_episode_model_attempt() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'EPISODE_RUNTIME_HISTORY_IMMUTABLE';
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status != 'PREPARED' OR NEW.reply_json IS NOT NULL
                        OR NEW.reply_hash IS NOT NULL
                        OR jsonb_typeof(NEW.request_json::jsonb) IS DISTINCT FROM 'object'
                        OR (NEW.request_json::jsonb ->> 'request_id') IS DISTINCT FROM NEW.request_id
                        OR (NEW.request_json::jsonb ->> 'episode_id') IS DISTINCT FROM NEW.episode_id
                        OR (NEW.request_json::jsonb ->> 'model') IS NULL THEN
                        RAISE EXCEPTION 'EPISODE_RUNTIME_FENCE_DENIED';
                    END IF;
                ELSE
                    IF NEW.request_id != OLD.request_id OR NEW.episode_id != OLD.episode_id
                        OR NEW.request_json != OLD.request_json OR NEW.input_hash != OLD.input_hash
                        OR NOT (
                            (OLD.status = 'PREPARED' AND NEW.status = 'DISPATCHING')
                            OR (OLD.status = 'DISPATCHING' AND NEW.status IN ('COMPLETE', 'UNCERTAIN'))
                        ) THEN
                        RAISE EXCEPTION 'EPISODE_RUNTIME_FENCE_DENIED';
                    END IF;
                    IF NEW.status = 'COMPLETE' AND (
                        NEW.reply_json IS NULL OR NEW.reply_hash IS NULL
                        OR jsonb_typeof(NEW.reply_json::jsonb) IS DISTINCT FROM 'object'
                        OR (NEW.reply_json::jsonb ->> 'request_id') IS DISTINCT FROM NEW.request_id
                        OR (NEW.reply_json::jsonb ->> 'input_hash') IS DISTINCT FROM NEW.input_hash
                        OR (NEW.reply_json::jsonb ->> 'model') IS DISTINCT FROM (NEW.request_json::jsonb ->> 'model')
                    ) THEN
                        RAISE EXCEPTION 'EPISODE_RUNTIME_FENCE_DENIED';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER safe_episode_model_attempt_fence
            BEFORE INSERT OR UPDATE OR DELETE ON safe_episode_model_attempts
            FOR EACH ROW EXECUTE FUNCTION safe_guard_episode_model_attempt()
        """)
        op.execute("""
            CREATE FUNCTION safe_guard_episode_private_context() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'EPISODE_RUNTIME_HISTORY_IMMUTABLE';
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.version != 0 OR NEW.active_observation IS NOT NULL THEN
                        RAISE EXCEPTION 'EPISODE_RUNTIME_FENCE_DENIED';
                    END IF;
                ELSE
                    IF NEW.episode_id != OLD.episode_id OR NEW.version != OLD.version + 1
                        OR NOT (
                            (OLD.active_observation IS NULL AND NEW.active_observation IS NOT NULL
                             AND NEW.context_json = OLD.context_json AND NEW.context_hash = OLD.context_hash)
                            OR (OLD.active_observation IS NOT NULL AND NEW.active_observation IS NULL)
                        ) THEN
                        RAISE EXCEPTION 'EPISODE_RUNTIME_FENCE_DENIED';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER safe_episode_private_context_fence
            BEFORE INSERT OR UPDATE OR DELETE ON safe_episode_private_contexts
            FOR EACH ROW EXECUTE FUNCTION safe_guard_episode_private_context()
        """)
    else:
        raise RuntimeError("unsupported episode inference database")


def downgrade() -> None:
    raise RuntimeError("episode evidence migrations are forward-only")
