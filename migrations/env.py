"""M2 PostgreSQL migration environment; SQLite is for isolated tests only."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, inspect, pool

from safe_agent.storage.models import Base

config = context.config
metadata = Base.metadata
url = os.environ.get("SAFE_AGENT_DATABASE_URL")
if not url or not (url.startswith("postgresql+psycopg://") or url.startswith("sqlite+pysqlite://")):
    raise RuntimeError("SAFE_AGENT_DATABASE_URL must specify PostgreSQL (or isolated test SQLite)")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                # Alembic defaults version_num to VARCHAR(32), but this
                # repository already has immutable revision IDs longer than
                # 32 characters (beginning with 0004_tal_office_execution_authorization).
                # Enlarge ONLY Alembic's tracking field BEFORE any revision
                # executes. Never rename/change an existing revision or fork
                # the selected 0009_inference_cost_accounts migration.
                inspector = inspect(connection)
                if not inspector.has_table("alembic_version"):
                    connection.exec_driver_sql(
                        "CREATE TABLE alembic_version ("
                        "version_num VARCHAR(128) NOT NULL, "
                        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                    )
                else:
                    columns = inspector.get_columns("alembic_version")
                    if (len(columns) != 1 or columns[0]["name"] != "version_num"):
                        raise RuntimeError("unexpected Alembic version schema")
                    old_length = getattr(columns[0]["type"], "length", None)
                    if old_length is not None and old_length < 128:
                        connection.exec_driver_sql(
                            "ALTER TABLE alembic_version "
                            "ALTER COLUMN version_num TYPE VARCHAR(128)"
                        )
                connection.commit()
            context.configure(connection=connection, target_metadata=metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
