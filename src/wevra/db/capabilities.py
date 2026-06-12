from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
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

    @classmethod
    def from_database_url(cls, database_url: str) -> SqlAlchemyDatabaseCapability:
        database = create_database(database_url)
        return cls({name: database for name in DEFAULT_CONNECTION_NAMES})

    def session(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]:
        self._connection(name)
        return self._session_scope(name)

    def transaction(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[AsyncSession]:
        self._connection(name)
        return self._transaction_scope(name)

    async def close(self) -> None:
        closed: set[int] = set()
        for database in self._connections.values():
            identity = id(database)
            if identity in closed:
                continue
            await close_database(database)
            closed.add(identity)

    @asynccontextmanager
    async def _session_scope(
        self,
        name: str,
    ) -> AsyncIterator[AsyncSession]:
        async with self._connection(name).session_factory() as session:
            yield session

    @asynccontextmanager
    async def _transaction_scope(
        self,
        name: str,
    ) -> AsyncIterator[AsyncSession]:
        async with self._connection(name).session_factory() as session:
            async with session.begin():
                yield session

    def _connection(self, name: str) -> Database:
        try:
            return self._connections[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._connections))
            raise DatabaseCapabilityError(
                f"Unknown database connection {name!r}. "
                f"Available connections: {available}."
            ) from exc
