from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from wybra.core.logging import configure_logging, default_logging_config
from wybra.db.alembic_attributes import (
    LOGGING_CONFIG_ATTRIBUTE,
    LOGGING_CONFIGURED_ATTRIBUTE,
)
from wybra.db.migration_metadata import load_model_metadata
from wybra.db.urls import is_supported_database_url, resolve_database_url

config = context.config


def configure_migration_logging() -> None:
    if config.attributes.get(LOGGING_CONFIGURED_ATTRIBUTE) is True:
        return

    configured_logging = config.attributes.get(LOGGING_CONFIG_ATTRIBUTE)
    if configured_logging is None:
        configured_logging = default_logging_config()
    configure_logging(configured_logging)
    config.attributes[LOGGING_CONFIGURED_ATTRIBUTE] = True


def _project_root() -> Path:
    configured_project_root = config.get_main_option("project_root")
    if configured_project_root is not None and configured_project_root.strip():
        return Path(configured_project_root.strip()).resolve()

    return Path.cwd()


def _app_config_path() -> Path | None:
    configured_path = config.get_main_option("app_config")
    if configured_path is None or not configured_path.strip():
        return None

    return Path(configured_path.strip())


target_metadata = load_model_metadata(
    project_root=_project_root(),
    config_path=_app_config_path(),
)


def _database_url() -> str:
    explicit_url = context.get_x_argument(as_dictionary=True).get("database_url")
    for configured_url in (
        explicit_url,
        config.get_main_option("sqlalchemy.url"),
    ):
        if configured_url and configured_url.strip():
            return _validated_database_url(
                resolve_database_url(configured_url.strip(), _project_root())
            )

    raise RuntimeError(
        "Alembic database URL is not configured. Set sqlalchemy.url or pass "
        "-x database_url=<url>."
    )


def _validated_database_url(database_url: str) -> str:
    if is_supported_database_url(database_url):
        return database_url

    raise RuntimeError(
        "Alembic database URL uses an unsupported driver. "
        "Use sqlite+aiosqlite:// or postgresql+asyncpg://."
    )


def run_migrations_offline() -> None:
    configure_migration_logging()
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    configure_migration_logging()
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_with_connection(connection: Connection) -> None:
    do_run_migrations(connection)


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        run_migrations_with_connection(existing_connection)
    else:
        asyncio.run(run_async_migrations())
