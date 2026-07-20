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

from wybra.diagnostics.context import (
    record_sql_query,
    record_topic,
)
from wybra.events import (
    BEGIN,
    COMMIT,
    EVT_SQL,
    RELEASE,
    ROLLBACK,
    SAVEPOINT,
    TRANSACTION,
    EventScope,
    EventSegment,
)

TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE: Final = (
    "_wybra_diagnostics_tortoise_instrumented"
)
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


def instrument_tortoise_context(context: TortoiseContext) -> None:
    """Record SQL timings for all currently configured Tortoise connections."""

    for connection in context.connections.all():
        instrument_tortoise_connection(connection)


def instrument_tortoise_connection(connection: BaseDBAsyncClient) -> None:
    """Wrap a Tortoise connection once so diagnostics can observe SQL."""

    if getattr(connection, TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE, False):
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
                operation=method_name.removeprefix("execute_"),
            ),
        )
    transaction_factory = getattr(connection, _TRANSACTION_FACTORY_METHOD, None)
    if transaction_factory is not None:
        setattr(
            connection,
            _TRANSACTION_FACTORY_METHOD,
            _instrument_transaction_factory(
                cast(_TransactionFactory, transaction_factory)
            ),
        )
    setattr(connection, TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE, True)


def _instrument_query_method(
    method: _AsyncQueryMethod,
    *,
    operation: str,
) -> _AsyncQueryMethod:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        statement = _statement_from_call(args, kwargs)
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
            record_sql_query(
                statement,
                duration_seconds=time.perf_counter() - started,
                result=result,
                operation=operation,
                result_count=_result_count(value, operation=operation),
                inserted_id=_inserted_id(value, operation=operation),
            )

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

    async def __aenter__(self) -> BaseDBAsyncClient:
        connection = await self._context.__aenter__()
        instrument_tortoise_connection(connection)
        self._kind = _transaction_kind(connection)
        record_topic(
            "trace",
            _transaction_topic(self._kind, BEGIN),
            attributes={"connection": connection.connection_name},
        )
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        result = await self._context.__aexit__(exc_type, exc_value, traceback)
        if exc_type is not None:
            outcome = "rollback"
        elif self._kind == "savepoint":
            outcome = "release"
        else:
            outcome = "commit"
        record_topic(
            "trace",
            _transaction_topic(
                self._kind,
                RELEASE
                if outcome == "release"
                else COMMIT
                if outcome == "commit"
                else ROLLBACK,
            ),
        )
        return result


def _statement_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    query = kwargs.get("query")
    if isinstance(query, str):
        return query
    if args and isinstance(args[0], str):
        return args[0]
    return "<unknown>"


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


def _transaction_topic(kind: str, outcome: EventSegment) -> EventScope:
    root = EVT_SQL(SAVEPOINT if kind == "savepoint" else TRANSACTION)
    return root(outcome)


def _transaction_kind(connection: BaseDBAsyncClient) -> str:
    if getattr(connection, "_savepoint", None) is not None:
        return "savepoint"
    return "transaction"


__all__ = (
    "TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE",
    "instrument_tortoise_connection",
    "instrument_tortoise_context",
)
