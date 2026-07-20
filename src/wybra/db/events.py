"""Typed, secret-safe observations produced by Wybra database boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from wybra.events import (
    CONNECTION,
    SAVEPOINT,
    SQL_STATEMENT,
    TRANSACTION,
    Event,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseConnectionEvent(Event):
    """Observation of a configured database connection boundary."""

    kind: ClassVar[EventSegment] = CONNECTION

    connection_name: str
    outcome: str


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


__all__ = (
    "DatabaseConnectionEvent",
    "DatabaseSavepointEvent",
    "DatabaseStatementEvent",
    "DatabaseTransactionEvent",
)
