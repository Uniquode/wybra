from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
)
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.core.exceptions import ConfigurationError
from wybra.db.persistence import Database, close_database, create_database
from wybra.db.routing import DbConnection, DbRoute
from wybra.db.settings import ResolvedDatabaseRouting

logger = logging.getLogger(__name__)


class DatabaseCapabilityError(RuntimeError):
    """Raised when a database capability operation cannot be completed."""


@runtime_checkable
class DatabaseCapability(Protocol):
    """Public database capability exposed through ``Site``."""

    def database(self, name: str = "default") -> DbConnection: ...

    async def close(self) -> None: ...


@runtime_checkable
class TortoiseRouteAdapter(Protocol):
    """Internal bridge from opaque routes to Tortoise client operations."""

    def _connection_for(self, route: DbRoute) -> BaseDBAsyncClient: ...

    def _transaction_for(
        self,
        route: DbRoute,
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]: ...


def tortoise_connection(
    capability: DatabaseCapability,
    route: DbRoute,
) -> BaseDBAsyncClient:
    """Resolve an opaque route through Wybra's internal Tortoise adapter."""
    return _tortoise_adapter(capability)._connection_for(route)


def tortoise_connection_for_route(
    connection: DbConnection,
    route: DbRoute,
) -> BaseDBAsyncClient:
    """Resolve a route through the capability bound to a database connection."""
    capability = connection._capability
    if not isinstance(capability, DatabaseCapability):
        raise DatabaseCapabilityError(
            "Database connection has no resolvable database capability."
        )
    return tortoise_connection(capability, route)


def tortoise_transaction_for_route(
    connection: DbConnection,
    route: DbRoute,
) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
    """Open a route-pinned transaction through a bound database capability."""
    capability = connection._capability
    if not isinstance(capability, DatabaseCapability):
        raise DatabaseCapabilityError(
            "Database connection has no resolvable database capability."
        )
    return tortoise_transaction(capability, route)


def tortoise_transaction(
    capability: DatabaseCapability,
    route: DbRoute,
) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
    """Open a transaction pinned to one opaque route's physical connection."""
    return _tortoise_adapter(capability)._transaction_for(route)


@dataclass(frozen=True, slots=True)
class TortoiseDatabaseCapability:
    _database: Database
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    async def from_database_routing(
        cls,
        database_routing: ResolvedDatabaseRouting,
        *,
        modules: Sequence[str],
    ) -> TortoiseDatabaseCapability:
        database = await create_database(
            database_routing.instances[0].connection,
            modules=modules,
            routing=database_routing,
        )
        return cls(database)

    def database(self, name: str = "default") -> DbConnection:
        self._require_open()
        try:
            connection = self._database.routes.connection(name)
            return DbConnection(connection._registry, connection.name, self)
        except ConfigurationError as exc:
            raise DatabaseCapabilityError(str(exc)) from exc

    def _connection_for(self, route: DbRoute) -> BaseDBAsyncClient:
        self._require_open()
        return self._database.connection_for(route)

    def _transaction_for(
        self,
        route: DbRoute,
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
        self._require_open()
        return self._database.transaction_for(route)

    def context(self) -> AbstractContextManager[None]:
        self._require_open()
        return self._context_scope()

    async def generate_schemas(self) -> None:
        self._require_open()
        with self._database.context:
            await self._database.context.generate_schemas()

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await close_database(self._database)
        except Exception as exc:
            logger.exception(
                "Database close failed",
                extra={
                    "database_identity": id(self._database),
                    "error_type": type(exc).__name__,
                },
            )
            raise DatabaseCapabilityError(
                "Database close failed: error_count=1."
            ) from exc

        object.__setattr__(self, "_closed", True)

    @contextmanager
    def _context_scope(self) -> Iterator[None]:
        with self._database.context:
            yield

    def _require_open(self) -> None:
        if self._closed:
            raise DatabaseCapabilityError("Database capability is closed.")


def _tortoise_adapter(capability: DatabaseCapability) -> TortoiseRouteAdapter:
    if isinstance(capability, TortoiseRouteAdapter):
        return capability
    raise DatabaseCapabilityError(
        "Database capability does not provide the internal Tortoise route adapter."
    )
