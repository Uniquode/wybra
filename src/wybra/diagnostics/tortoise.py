from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Final, cast

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.context import TortoiseContext

from wybra.diagnostics.context import current_diagnostics, record_sql_query

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
    """Wrap a Tortoise connection once so request diagnostics can observe SQL."""

    if getattr(connection, TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE, False):
        return
    for method_name in _QUERY_METHODS:
        method = getattr(connection, method_name, None)
        if method is None:
            continue
        setattr(
            connection,
            method_name,
            _instrument_query_method(cast(_AsyncQueryMethod, method)),
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


def _instrument_query_method(method: _AsyncQueryMethod) -> _AsyncQueryMethod:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_diagnostics() is None:
            return await method(*args, **kwargs)
        statement = _statement_from_call(args, kwargs)
        started = time.perf_counter()
        result = "ok"
        try:
            return await method(*args, **kwargs)
        except Exception:
            result = "error"
            raise
        finally:
            record_sql_query(
                statement,
                duration_seconds=time.perf_counter() - started,
                result=result,
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

    async def __aenter__(self) -> BaseDBAsyncClient:
        connection = await self._context.__aenter__()
        instrument_tortoise_connection(connection)
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return await self._context.__aexit__(exc_type, exc_value, traceback)


def _statement_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    query = kwargs.get("query")
    if isinstance(query, str):
        return query
    if args and isinstance(args[0], str):
        return args[0]
    return "<unknown>"


__all__ = (
    "TORTOISE_DIAGNOSTICS_INSTRUMENTED_ATTRIBUTE",
    "instrument_tortoise_connection",
    "instrument_tortoise_context",
)
