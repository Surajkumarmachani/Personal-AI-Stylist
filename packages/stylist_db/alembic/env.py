"""Alembic environment.

Runs migrations through asyncpg rather than a sync driver, so the project ships
ONE Postgres driver instead of two (asyncpg for the app, psycopg2 only for
Alembic). Fewer drivers means fewer version matrices and no chance of the two
disagreeing about type adaptation.

Migrations run as the OWNER/migration role — never as `stylist_app`, which is
deliberately NOBYPASSRLS and cannot manage policies. MIGRATION_DATABASE_URL is
the owner DSN; DATABASE_URL (the app role) is intentionally not used here.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from stylist_db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_dsn() -> str | None:
    dsn = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    # Normalise whatever driver was supplied to asyncpg.
    if dsn.startswith("postgresql+"):
        dsn = "postgresql://" + dsn.split("://", 1)[1]
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


dsn = _resolve_dsn()
if dsn:
    config.set_main_option("sqlalchemy.url", dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
