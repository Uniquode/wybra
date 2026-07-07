"""Transaction helpers for auth persistence operations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.transactions import in_transaction


@asynccontextmanager
async def auth_savepoint(
    connection: BaseDBAsyncClient,
) -> AsyncIterator[BaseDBAsyncClient]:
    """Yield a nested transaction scoped to the active auth connection.

    PostgreSQL aborts a whole transaction after an integrity error unless the
    failed statement is isolated in a savepoint. Tortoise implements nested
    transactions as savepoints when a transaction is already active.
    """

    async with in_transaction(connection.connection_name) as savepoint_connection:
        yield savepoint_connection
