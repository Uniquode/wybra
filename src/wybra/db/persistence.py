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

TORTOISE_PRIVATE_API_ERROR = (
    "Wybra database shutdown requires Tortoise connection internals that are "
    "missing from the installed tortoise-orm version. Use the pinned Wybra "
    "tortoise-orm version or upgrade Wybra with a compatible shutdown adapter."
)
_MISSING_CREATE_CONNECTION = object()

__all__ = (
    "Database",
    "close_database",
    "close_database_connections",
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
    _create_connection_restore: object = field(
        compare=False,
        repr=False,
    )
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
    create_connection_restore, tracked_connections = _track_created_connections(
        context.connections
    )
    return Database(
        context=context,
        config=config,
        _create_connection_restore=create_connection_restore,
        _connections=tracked_connections,
    )


async def close_database(database: Database) -> None:
    context = database.context
    with context:
        await close_database_connections(database)
        if not hasattr(context, "_connections"):
            raise ConfigurationError(TORTOISE_PRIVATE_API_ERROR)
        context._connections = None
    if not hasattr(tortoise_context, "_global_context"):
        raise ConfigurationError(TORTOISE_PRIVATE_API_ERROR)
    if tortoise_context._global_context is context:
        tortoise_context._global_context = None


async def close_database_connections(database: Database) -> None:
    with database.context:
        await _close_tracked_connections(database)
        _discard_stored_connections(database.context.connections)
        _restore_create_connection(database)


def _track_created_connections(
    connections: ConnectionHandler,
) -> tuple[object, list[BaseDBAsyncClient]]:
    # Tortoise close_all() calls get(), which can replace cross-loop clients
    # during shutdown. Track created clients so close never creates replacements.
    # Remove this shim once the upstream Tortoise close_all() fix is released
    # and Wybra pins a version that includes it.
    copy_storage = _copy_tortoise_connection_storage(connections)
    create_connection = getattr(connections, "_create_connection", None)
    if not callable(create_connection):
        raise ConfigurationError(TORTOISE_PRIVATE_API_ERROR)
    create_connection_restore = connections.__dict__.get(
        "_create_connection",
        _MISSING_CREATE_CONNECTION,
    )
    tracked_connections = list(copy_storage.values())

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
    return create_connection_restore, tracked_connections


def _restore_create_connection(database: Database) -> None:
    if database._create_connection_restore is _MISSING_CREATE_CONNECTION:
        database.context.connections.__dict__.pop("_create_connection", None)
        return
    database.context.connections._create_connection = cast(
        Any,
        database._create_connection_restore,
    )


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
    if not hasattr(connections, "_db_config"):
        raise ConfigurationError(TORTOISE_PRIVATE_API_ERROR)
    if connections._db_config is None:
        return

    for alias in tuple(_copy_tortoise_connection_storage(connections)):
        connections.discard(alias)
    for alias in tuple(connections.db_config):
        connections.discard(alias)


def _copy_tortoise_connection_storage(
    connections: ConnectionHandler,
) -> dict[str, BaseDBAsyncClient]:
    copy_storage = getattr(connections, "_copy_storage", None)
    if not callable(copy_storage):
        raise ConfigurationError(TORTOISE_PRIVATE_API_ERROR)
    return copy_storage()


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
