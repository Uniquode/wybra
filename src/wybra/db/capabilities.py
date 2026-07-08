from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.transactions import in_transaction

from wybra.db.persistence import Database, close_database, create_database
from wybra.db.settings import ResolvedDatabaseConnection

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

    def connection(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> BaseDBAsyncClient: ...

    def transaction(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TortoiseDatabaseCapability:
    _database: Database
    _connection_aliases: Mapping[str, str]
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    async def from_database_connection(
        cls,
        database_connection: ResolvedDatabaseConnection,
        *,
        modules: Sequence[str],
    ) -> TortoiseDatabaseCapability:
        database = await create_database(
            database_connection,
            modules=modules,
        )
        connection_aliases = {
            name: DEFAULT_CONNECTION_NAME for name in DEFAULT_CONNECTION_NAMES
        }
        return cls(database, connection_aliases)

    def connection(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> BaseDBAsyncClient:
        self._require_open()
        return self._database.connection(self._connection_name(name))

    def transaction(
        self,
        name: str = DEFAULT_CONNECTION_NAME,
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
        self._require_open()
        return self._transaction_scope(self._connection_name(name))

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

    @asynccontextmanager
    async def _transaction_scope(
        self,
        connection_name: str,
    ) -> AsyncIterator[BaseDBAsyncClient]:
        with self._database.context:
            async with in_transaction(connection_name) as connection:
                yield connection

    @contextmanager
    def _context_scope(self) -> Iterator[None]:
        with self._database.context:
            yield

    def _connection_name(self, name: str) -> str:
        try:
            return self._connection_aliases[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._connection_aliases))
            raise DatabaseCapabilityError(
                "Unknown database connection: "
                f"connection_name={name}, available_connections={available}."
            ) from exc

    def _require_open(self) -> None:
        if self._closed:
            raise DatabaseCapabilityError("Database capability is closed.")
