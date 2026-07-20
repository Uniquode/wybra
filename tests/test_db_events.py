from __future__ import annotations

import asyncio

import pytest
from tortoise.transactions import in_transaction

from wybra.db.events import (
    DatabaseConnectionEvent,
    DatabaseSavepointEvent,
    DatabaseStatementEvent,
    DatabaseTransactionEvent,
)
from wybra.db.exceptions import OperationalError
from wybra.db.persistence import close_database, create_database
from wybra.events import EVT_SQL, Event, EventDispatcher, scope


class TestDatabaseEvents:
    @pytest.mark.anyio
    async def test_statement_event_uses_sql_statement_scope_and_safe_payload(
        self,
    ) -> None:
        @scope(EVT_SQL)
        async def create_event() -> DatabaseStatementEvent:
            return DatabaseStatementEvent(
                connection_name="default",
                operation="query",
                duration_seconds=0.01,
                result="ok",
                result_count=1,
            )

        event = await create_event()

        assert str(event.scope) == "sql.statement"
        assert event.connection_name == "default"
        assert event.operation == "query"
        assert event.result_count == 1
        assert not hasattr(event, "statement")

    @pytest.mark.anyio
    async def test_transaction_event_uses_sql_transaction_scope_and_outcome(
        self,
    ) -> None:
        @scope(EVT_SQL)
        async def create_event() -> DatabaseTransactionEvent:
            return DatabaseTransactionEvent(
                connection_name="default",
                transaction_kind="transaction",
                outcome="commit",
            )

        event = await create_event()

        assert str(event.scope) == "sql.transaction"
        assert event.outcome == "commit"

    @pytest.mark.anyio
    async def test_database_setup_and_statement_execution_publish_safe_events(
        self,
    ) -> None:
        dispatcher = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        dispatcher.subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:",
            modules=("wybra.sessions",),
            events=dispatcher,
        )
        try:
            with database.context:
                await database.connection().execute_query("select 1")
        finally:
            await close_database(database)

        connection_event = next(
            event for event in observed if isinstance(event, DatabaseConnectionEvent)
        )
        statement_event = next(
            event for event in observed if isinstance(event, DatabaseStatementEvent)
        )
        assert connection_event.outcome == "configured"
        assert statement_event.operation == "query"
        assert statement_event.result == "ok"
        assert statement_event.result_count == 1
        assert not hasattr(statement_event, "statement")

    @pytest.mark.anyio
    async def test_transactions_savepoints_and_rollbacks_publish_outcomes(
        self,
    ) -> None:
        dispatcher = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        dispatcher.subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:",
            modules=("wybra.sessions",),
            events=dispatcher,
        )
        try:
            with database.context:
                async with in_transaction("default") as connection:
                    async with in_transaction(connection.connection_name):
                        await connection.execute_query("select 1")
                with pytest.raises(ValueError, match="rollback"):
                    async with in_transaction("default"):
                        raise ValueError("rollback")
        finally:
            await close_database(database)

        transactions = [
            event for event in observed if isinstance(event, DatabaseTransactionEvent)
        ]
        savepoints = [
            event for event in observed if isinstance(event, DatabaseSavepointEvent)
        ]
        assert [event.outcome for event in transactions] == [
            "begin",
            "commit",
            "begin",
            "rollback",
        ]
        assert [event.outcome for event in savepoints] == ["begin", "release"]

    @pytest.mark.anyio
    async def test_cancelled_transaction_preserves_cancellation_and_records_failure(
        self,
    ) -> None:
        dispatcher = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        dispatcher.subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:", modules=("wybra.sessions",), events=dispatcher
        )
        try:
            with database.context:
                with pytest.raises(asyncio.CancelledError):
                    async with in_transaction("default"):
                        raise asyncio.CancelledError()
        finally:
            await close_database(database)

        transactions = [
            event for event in observed if isinstance(event, DatabaseTransactionEvent)
        ]
        assert [event.outcome for event in transactions] == ["begin", "rollback"]

    @pytest.mark.anyio
    async def test_failed_statement_event_preserves_the_database_error(self) -> None:
        dispatcher = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        dispatcher.subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:",
            modules=("wybra.sessions",),
            events=dispatcher,
        )
        try:
            with database.context:
                with pytest.raises(OperationalError):
                    await database.connection().execute_query("not valid sql")
        finally:
            await close_database(database)

        statement_event = next(
            event for event in observed if isinstance(event, DatabaseStatementEvent)
        )
        assert statement_event.result == "error"
