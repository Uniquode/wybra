"""Database event contracts and producer descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    CONNECTION,
    EVT_SQL,
    SAVEPOINT,
    SQL_STATEMENT,
    TRANSACTION,
    Event,
    EventOutcome,
    EventSegment,
    observe,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseConnectionEvent(Event):
    """Observation of a configured database connection boundary."""

    kind: ClassVar[EventSegment] = CONNECTION
    connection_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseStatementEvent(Event):
    """Observation of one completed database statement without parameters."""

    kind: ClassVar[EventSegment] = SQL_STATEMENT
    connection_name: str
    operation: str
    duration_seconds: float
    result: str
    result_count: int | None = None
    inserted_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseTransactionEvent(Event):
    """Observation of a transaction boundary."""

    kind: ClassVar[EventSegment] = TRANSACTION
    connection_name: str
    transaction_kind: str
    outcome: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseSavepointEvent(Event):
    """Observation of a savepoint boundary."""

    kind: ClassVar[EventSegment] = SAVEPOINT
    connection_name: str
    outcome: str


def database_statement_event(
    _call: BoundArguments,
    outcome: EventOutcome | None,
    connection_name: str,
    operation: str,
) -> Event | None:
    """Build a terminal SQL statement observation without SQL text or values."""
    if outcome is None:
        return None
    value = outcome.result
    return DatabaseStatementEvent(
        topic=EVT_SQL(SQL_STATEMENT),
        connection_name=connection_name,
        operation=operation,
        duration_seconds=outcome.duration_seconds,
        result="ok" if outcome.succeeded else "error",
        result_count=_result_count(value, operation=operation),
        inserted_id=_inserted_id(value, operation=operation),
    )


def database_connection_event(
    call: BoundArguments, outcome: EventOutcome | None
) -> Event | None:
    """Build a configured-connection observation from its opaque name."""
    if outcome is None:
        return None
    connection_name = call.arguments["connection_name"]
    if not isinstance(connection_name, str):
        raise TypeError("Database connection events require a connection name.")
    return DatabaseConnectionEvent(
        topic=EVT_SQL(CONNECTION),
        connection_name=connection_name,
    )


@observe(database_connection_event)
async def record_database_connection(connection_name: str) -> None:
    """Record that an opaque configured connection became available."""
    del connection_name


def database_transaction_event(
    call: BoundArguments, outcome: EventOutcome | None
) -> Event | None:
    """Build a transaction or savepoint event from opaque boundary metadata."""
    if outcome is None:
        return None
    arguments = call.arguments
    connection_name = arguments["connection_name"]
    transaction_kind = arguments["transaction_kind"]
    transaction_outcome = arguments["outcome"]
    if not all(
        isinstance(value, str)
        for value in (connection_name, transaction_kind, transaction_outcome)
    ):
        raise TypeError("Database transaction events require string metadata.")
    if transaction_kind == "savepoint":
        return DatabaseSavepointEvent(
            topic=EVT_SQL(SAVEPOINT),
            connection_name=connection_name,
            outcome=transaction_outcome,
        )
    return DatabaseTransactionEvent(
        topic=EVT_SQL(TRANSACTION),
        connection_name=connection_name,
        transaction_kind=transaction_kind,
        outcome=transaction_outcome,
    )


def _result_count(value: object | None, *, operation: str) -> int | None:
    if operation == "insert":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], int):
        return value[0]
    return None


def _inserted_id(value: object | None, *, operation: str) -> int | None:
    return value if operation == "insert" and isinstance(value, int) else None


__all__ = (
    "DatabaseConnectionEvent",
    "DatabaseSavepointEvent",
    "DatabaseStatementEvent",
    "DatabaseTransactionEvent",
    "database_connection_event",
    "database_statement_event",
    "database_transaction_event",
    "record_database_connection",
)
