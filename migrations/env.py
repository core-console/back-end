"""Alembic environment using the application's validated async database URL."""

import asyncio
import sys
from logging.config import fileConfig
from typing import cast

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from core_console.config import load_settings
from core_console.modules.finance.models import FinanceAccount, FinanceCategory, FinanceLedger
from core_console.modules.users.models import User

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = FinanceAccount.metadata
assert target_metadata is FinanceCategory.metadata
assert target_metadata is FinanceLedger.metadata
assert target_metadata is User.metadata


def configured_database_url() -> str:
    """Return the validated URL or fail before Alembic attempts any connection."""

    database_url = load_settings().database_url_value()
    if database_url is None:
        raise RuntimeError(
            "DATABASE_URL is required for Alembic; set an explicit postgresql+psycopg:// URL."
        )
    return database_url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    context.configure(
        url=configured_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection, /) -> None:
    """Configure a synchronous Alembic context over an async connection."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create and dispose the migration-only async engine."""

    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = configured_database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations with an async SQLAlchemy engine or shared connection."""

    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        do_run_migrations(cast(Connection, shared_connection))
    elif sys.platform == "win32":
        asyncio.run(
            run_async_migrations(),
            loop_factory=asyncio.SelectorEventLoop,
        )
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
