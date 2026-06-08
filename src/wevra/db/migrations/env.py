from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from wevra.db.migrate import (
    DEFAULT_DATABASE_URL_CONFIG_KEY,
    DEFAULT_MODULES_CONFIG_KEY,
)
from wevra.db.migration_metadata import load_model_metadata
from wevra.db.urls import is_supported_database_url, resolve_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _project_root() -> Path:
    if config.config_file_name is not None:
        return Path(config.config_file_name).resolve().parent

    return Path.cwd()


def _app_config_path() -> Path | None:
    configured_path = config.get_main_option("app_config")
    if configured_path is None or not configured_path.strip():
        return None

    return Path(configured_path.strip())


def _default_modules() -> tuple[str, ...] | None:
    configured_modules = config.get_main_option(DEFAULT_MODULES_CONFIG_KEY)
    if configured_modules is None or not configured_modules.strip():
        return None

    modules = tuple(
        module.strip()
        for module in configured_modules.replace(os.pathsep, ",").split(",")
        if module.strip()
    )
    return modules or None


target_metadata = load_model_metadata(
    project_root=_project_root(),
    config_path=_app_config_path(),
    default_modules=_default_modules(),
)


def _database_url() -> str:
    explicit_url = context.get_x_argument(as_dictionary=True).get("database_url")
    for configured_url in (
        explicit_url,
        config.get_main_option("sqlalchemy.url"),
        config.get_main_option(DEFAULT_DATABASE_URL_CONFIG_KEY),
    ):
        if configured_url and configured_url.strip():
            return _validated_database_url(
                resolve_database_url(configured_url.strip(), _project_root())
            )

    raise RuntimeError(
        "Alembic database URL is not configured. Set sqlalchemy.url, "
        f"{DEFAULT_DATABASE_URL_CONFIG_KEY}, or pass -x database_url=<url>."
    )


def _validated_database_url(database_url: str) -> str:
    if is_supported_database_url(database_url):
        return database_url

    raise RuntimeError(
        "Alembic database URL uses an unsupported driver. "
        "Use sqlite+aiosqlite:// or postgresql+asyncpg://."
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
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
