from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from types import MethodType
from typing import Any, Protocol, cast

import tortoise.context as tortoise_context
from tortoise import Tortoise
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.connection import ConnectionHandler
from tortoise.context import TortoiseContext

from wybra.core.exceptions import ConfigurationError
from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.tortoise import build_tortoise_config
from wybra.db.urls import (
    database_url_support_error,
    is_database_backend_available,
    is_memory_database_url,
    is_supported_database_url,
    sqlite_database_path,
)
from wybra.diagnostics.tortoise import instrument_tortoise_context

__all__ = (
    "Database",
    "close_database",
    "create_database",
    "is_memory_database_url",
    "is_supported_database_url",
    "sqlite_database_path",
)


class DatabaseSettings(Protocol):
    """Smallest settings shape accepted by reusable persistence helpers."""

    database_connection: ResolvedDatabaseConnection


@dataclass(frozen=True, slots=True)
class Database:
    context: TortoiseContext
    config: dict[str, object]
    _connections: list[BaseDBAsyncClient] = field(
        default_factory=list,
        compare=False,
        repr=False,
    )

    def connection(self, name: str = "default") -> BaseDBAsyncClient:
        return self.context.connections.get(name)


async def create_database(
    settings_or_url: DatabaseSettings | ResolvedDatabaseConnection | str,
    *,
    modules: Sequence[str],
    enable_global_fallback: bool = False,
) -> Database:
    database_connection = _database_connection_from(settings_or_url)
    if database_connection is None:
        database_url = _database_url_from(settings_or_url)
        if not is_supported_database_url(database_url):
            raise ConfigurationError(database_url_support_error(database_url))
        config = build_tortoise_config(
            database_url=database_url,
            modules=modules,
        )
    else:
        if not is_database_backend_available(database_connection.backend):
            raise ConfigurationError(
                database_url_support_error(f"{database_connection.backend.scheme}://")
            )
        config = build_tortoise_config(
            database_connection=database_connection,
            modules=modules,
        )

    context = await Tortoise.init(
        config=config,
        _enable_global_fallback=enable_global_fallback,
    )
    instrument_tortoise_context(context)
    return Database(
        context=context,
        config=config,
        _connections=_track_created_connections(context.connections),
    )


async def close_database(database: Database) -> None:
    context = database.context
    with context:
        await close_database_connections(database)
        context._connections = None
    if tortoise_context._global_context is context:
        tortoise_context._global_context = None


async def close_database_connections(database: Database) -> None:
    with database.context:
        await _close_tracked_connections(database)
        _discard_stored_connections(database.context.connections)


def _track_created_connections(
    connections: ConnectionHandler,
) -> list[BaseDBAsyncClient]:
    # Tortoise close_all() calls get(), which can replace cross-loop clients
    # during shutdown. Track created clients so close never creates replacements.
    tracked_connections = list(connections._copy_storage().values())
    create_connection = connections._create_connection

    def tracked_create_connection(
        _connections: ConnectionHandler,
        conn_alias: str,
    ) -> BaseDBAsyncClient:
        connection = create_connection(conn_alias)
        tracked_connections.append(connection)
        return connection

    connections._create_connection = cast(
        Any,
        MethodType(tracked_create_connection, connections),
    )
    return tracked_connections


async def _close_tracked_connections(database: Database) -> None:
    errors: list[Exception] = []
    for connection in tuple(database._connections):
        try:
            await connection.close()
        except Exception as exc:
            errors.append(exc)
    database._connections.clear()

    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("Database connection close failed.", errors)


def _discard_stored_connections(connections: ConnectionHandler) -> None:
    if connections._db_config is None:
        return

    for alias in tuple(connections._copy_storage()):
        connections.discard(alias)
    for alias in tuple(connections.db_config):
        connections.discard(alias)


def _database_connection_from(
    settings_or_url: DatabaseSettings | ResolvedDatabaseConnection | str,
) -> ResolvedDatabaseConnection | None:
    if isinstance(settings_or_url, ResolvedDatabaseConnection):
        return settings_or_url
    if isinstance(settings_or_url, str):
        return None
    return getattr(settings_or_url, "database_connection", None)


def _database_url_from(settings_or_url: object) -> str:
    if isinstance(settings_or_url, str):
        return settings_or_url

    database_url = getattr(settings_or_url, "database_url", None)
    if isinstance(database_url, str):
        return database_url
    raise ConfigurationError("Database URL is required.")
