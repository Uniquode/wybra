"""Version-sensitive private Tortoise SQL instrumentation.

This module is the sole location for private Tortoise client adaptation.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Final, cast

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.context import TortoiseContext

from wybra.db.events import (
    DatabaseSavepointEvent,
    DatabaseStatementEvent,
    DatabaseTransactionEvent,
)
from wybra.events import (
    EVT_SQL,
    EventsCapability,
    event_delivery_enabled,
    publish_observation,
    scoped,
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
    events: EventsCapability | None = None,
) -> None:
    """Instrument all currently configured Tortoise connections."""

    for connection in context.connections.all():
        instrument_tortoise_connection(connection, events)


def instrument_tortoise_connection(
    connection: BaseDBAsyncClient,
    events: EventsCapability | None = None,
) -> None:
    """Wrap a Tortoise connection once so events can observe SQL."""

    if getattr(connection, TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE, False):
        return
    if events is not None:
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
                    events=events,
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
                events,
            ),
        )
    setattr(connection, TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE, True)


def _instrument_query_method(
    method: _AsyncQueryMethod,
    *,
    connection_name: str,
    events: EventsCapability,
    operation: str,
) -> _AsyncQueryMethod:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not event_delivery_enabled(events):
            return await method(*args, **kwargs)
        started = time.perf_counter()
        result = "ok"
        value: Any = None
        try:
            value = await method(*args, **kwargs)
            return value
        except Exception:
            result = "error"
            raise
        finally:
            with scoped(EVT_SQL):
                await publish_observation(
                    events,
                    DatabaseStatementEvent(
                        connection_name=connection_name,
                        operation=operation,
                        duration_seconds=time.perf_counter() - started,
                        result=result,
                        result_count=_result_count(value, operation=operation),
                        inserted_id=_inserted_id(value, operation=operation),
                    ),
                    message="database statement event",
                )

    return wrapped


def _instrument_transaction_factory(
    method: _TransactionFactory,
    events: EventsCapability | None,
) -> _TransactionFactory:
    def wrapped(
        *args: Any, **kwargs: Any
    ) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
        return _InstrumentedTransactionContext(method(*args, **kwargs), events)

    return wrapped


class _InstrumentedTransactionContext(
    AbstractAsyncContextManager[BaseDBAsyncClient],
):
    def __init__(
        self,
        context: AbstractAsyncContextManager[BaseDBAsyncClient],
        events: EventsCapability | None,
    ) -> None:
        self._context = context
        self._events = events
        self._kind = "transaction"
        self._connection_name = ""

    async def __aenter__(self) -> BaseDBAsyncClient:
        connection = await self._context.__aenter__()
        instrument_tortoise_connection(connection, self._events)
        self._kind = _transaction_kind(connection)
        self._connection_name = connection.connection_name
        await _publish_transaction_observation(
            self._events,
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
                self._events,
                connection_name=self._connection_name,
                transaction_kind=self._kind,
                outcome=outcome,
                message="database transaction completion event",
            )


def _result_count(value: object, *, operation: str) -> int | None:
    if operation == "insert":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], int):
        return value[0]
    return None


def _inserted_id(value: object, *, operation: str) -> int | None:
    return value if operation == "insert" and isinstance(value, int) else None


async def _publish_transaction_observation(
    events: EventsCapability | None,
    *,
    connection_name: str,
    transaction_kind: str,
    outcome: str,
    message: str,
) -> None:
    if events is None or not event_delivery_enabled(events):
        return
    with scoped(EVT_SQL):
        if transaction_kind == "savepoint":
            event = DatabaseSavepointEvent(
                connection_name=connection_name,
                outcome=outcome,
            )
        else:
            event = DatabaseTransactionEvent(
                connection_name=connection_name,
                transaction_kind=transaction_kind,
                outcome=outcome,
            )
        await publish_observation(events, event, message=message)


def _transaction_kind(connection: BaseDBAsyncClient) -> str:
    if getattr(connection, "_savepoint", None) is not None:
        return "savepoint"
    return "transaction"


__all__ = (
    "TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE",
    "instrument_tortoise_connection",
    "instrument_tortoise_context",
)
