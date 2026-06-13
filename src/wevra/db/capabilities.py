from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from wevra.db.persistence import Database, close_database, create_database

DEFAULT_CONNECTION_NAME = "default"
READER_CONNECTION_NAME = "reader"
WRITER_CONNECTION_NAME = "writer"
DEFAULT_CONNECTION_NAMES = (
    DEFAULT_CONNECTION_NAME,
    READER_CONNECTION_NAME,
    WRITER_CONNECTION_NAME,
)
logger = logging.getLogger(__name__)


class DatabaseCapabilityError(RuntimeError):
    """Raised when a database capability operation cannot be completed."""


@runtime_checkable
class DatabaseCapability(Protocol):
    """Public database capability exposed through ``Site``."""

    def session(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]: ...

    def transaction(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SqlAlchemyDatabaseCapability:
    _connections: Mapping[str, Database]
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_database_url(cls, database_url: str) -> SqlAlchemyDatabaseCapability:
        """Create default reader/writer aliases backed by one database.

        This is the single-URL startup path. Use ``from_connections`` when a
        site needs distinct named database connections.
        """
        database = create_database(database_url)
        connection_aliases = {name: database for name in DEFAULT_CONNECTION_NAMES}
        return cls.from_connections(connection_aliases)

    @classmethod
    def from_connections(
        cls,
        connections: Mapping[str, Database],
    ) -> SqlAlchemyDatabaseCapability:
        return cls(dict(connections))

    def session(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]:
        self._require_open()
        return self._session_scope(self._connection(name))

    def transaction(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]:
        self._require_open()
        return self._transaction_scope(self._connection(name))

    async def close(self) -> None:
        """Close all underlying databases.

        Databases are deduplicated by object identity. When multiple connection
        names refer to the same ``Database`` instance, it is closed once.
        """
        if self._closed:
            return
        error_count = 0
        closed: set[int] = set()
        for database in self._connections.values():
            identity = id(database)
            if identity in closed:
                continue
            closed.add(identity)
            try:
                await close_database(database)
            except Exception as exc:
                error_count += 1
                logger.exception(
                    "Database close failed",
                    extra={
                        "database_identity": identity,
                        "error_type": type(exc).__name__,
                    },
                )

        if error_count:
            raise DatabaseCapabilityError(
                f"Database close failed: error_count={error_count}."
            )

        object.__setattr__(self, "_closed", True)

    @asynccontextmanager
    async def _session_scope(
        self,
        database: Database,
    ) -> AsyncIterator[AsyncSession]:
        async with database.session_factory() as session:
            yield session

    @asynccontextmanager
    async def _transaction_scope(
        self,
        database: Database,
    ) -> AsyncIterator[AsyncSession]:
        async with database.session_factory() as session:
            async with session.begin():
                yield session

    def _connection(self, name: str) -> Database:
        try:
            return self._connections[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._connections))
            raise DatabaseCapabilityError(
                "Unknown database connection: "
                f"connection_name={name}, available_connections={available}."
            ) from exc

    def _require_open(self) -> None:
        if self._closed:
            raise DatabaseCapabilityError("Database capability is closed.")
