"""M2 PostgreSQL migration environment; SQLite is for isolated tests only."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

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
            context.configure(connection=connection, target_metadata=metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
