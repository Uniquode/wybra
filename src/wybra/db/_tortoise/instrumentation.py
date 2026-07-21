"""Version-sensitive private Tortoise SQL instrumentation.

This module is the sole location for private Tortoise client adaptation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Final, cast

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.context import TortoiseContext

from wybra.events import observe
from wybra.events.db import (
    database_statement_event,
    database_transaction_event,
)

TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE: Final = "_wybra_events_tortoise_instrumented"
_TRANSACTION_FACTORY_METHOD: Final = "_in_transaction"
_QUERY_METHODS: Final = (
    "execute_insert",
    "execute_many",
    "execute_query",
    "execute_query_dict",
    "execute_query_dict_with_affected",
    "execute_script",
)


type _AsyncQueryMethod = Callable[..., Awaitable[Any]]
type _TransactionFactory = Callable[
    ...,
    AbstractAsyncContextManager[BaseDBAsyncClient],
]


def instrument_tortoise_context(
    context: TortoiseContext,
) -> None:
    """Instrument all currently configured Tortoise connections."""

    for connection in context.connections.all():
        instrument_tortoise_connection(connection)


def instrument_tortoise_connection(
    connection: BaseDBAsyncClient,
) -> None:
    """Wrap a Tortoise connection once so events can observe SQL."""

    if getattr(connection, TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE, False):
        return
    for method_name in _QUERY_METHODS:
        method = getattr(connection, method_name, None)
        if method is None:
            continue
        setattr(
            connection,
            method_name,
            _instrument_query_method(
                cast(_AsyncQueryMethod, method),
                connection_name=connection.connection_name,
                operation=method_name.removeprefix("execute_"),
            ),
        )
    transaction_factory = getattr(connection, _TRANSACTION_FACTORY_METHOD, None)
    if transaction_factory is not None:
        setattr(
            connection,
            _TRANSACTION_FACTORY_METHOD,
            _instrument_transaction_factory(
                cast(_TransactionFactory, transaction_factory),
            ),
        )
    setattr(connection, TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE, True)


def _instrument_query_method(
    method: _AsyncQueryMethod,
    *,
    connection_name: str,
    operation: str,
) -> _AsyncQueryMethod:
    @observe(database_statement_event, connection_name, operation)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        return await method(*args, **kwargs)

    return wrapped


def _instrument_transaction_factory(
    method: _TransactionFactory,
) -> _TransactionFactory:
    def wrapped(
        *args: Any, **kwargs: Any
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
        return _InstrumentedTransactionContext(method(*args, **kwargs))

    return wrapped


class _InstrumentedTransactionContext(
    AbstractAsyncContextManager[BaseDBAsyncClient],
):
    def __init__(
        self,
        context: AbstractAsyncContextManager[BaseDBAsyncClient],
    ) -> None:
        self._context = context
        self._kind = "transaction"
        self._connection_name = ""

    async def __aenter__(self) -> BaseDBAsyncClient:
        connection = await self._context.__aenter__()
        instrument_tortoise_connection(connection)
        self._kind = _transaction_kind(connection)
        self._connection_name = connection.connection_name
        await _publish_transaction_observation(
            connection_name=self._connection_name,
            transaction_kind=self._kind,
            outcome="begin",
            message="database transaction begin event",
        )
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        outcome = "failed"
        try:
            result = await self._context.__aexit__(exc_type, exc_value, traceback)
        except BaseException:
            raise
        else:
            if exc_type is not None:
                outcome = "rollback"
            elif self._kind == "savepoint":
                outcome = "release"
            else:
                outcome = "commit"
            return result
        finally:
            await _publish_transaction_observation(
                connection_name=self._connection_name,
                transaction_kind=self._kind,
                outcome=outcome,
                message="database transaction completion event",
            )


@observe(database_transaction_event)
async def _publish_transaction_observation(
    *,
    connection_name: str,
    transaction_kind: str,
    outcome: str,
    message: str,
) -> None:
    del connection_name, transaction_kind, outcome, message


def _transaction_kind(connection: BaseDBAsyncClient) -> str:
    if getattr(connection, "_savepoint", None) is not None:
        return "savepoint"
    return "transaction"


__all__ = (
    "TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE",
    "instrument_tortoise_connection",
    "instrument_tortoise_context",
)
