from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from tortoise.transactions import in_transaction

from wybra.config import MappingConfigSource
from wybra.db.exceptions import OperationalError
from wybra.db.persistence import close_database, create_database
from wybra.events._core import (
    EVT_SQL,
    SQL_STATEMENT,
    TRANSACTION,
    Event,
    EventsCapability,
)
from wybra.events.db import (
    DatabaseConnectionEvent,
    DatabaseSavepointEvent,
    DatabaseStatementEvent,
    DatabaseTransactionEvent,
)
from wybra.site import Site, start


async def _start_events_site() -> Site:
    return await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {"modules": (), "deployment_environment": "local"},
                "wybra.events": {"enabled": True},
            }
        ),
    )


class TestDatabaseEvents:
    @pytest.mark.anyio
    async def test_statement_event_uses_sql_statement_scope_and_safe_payload(
        self,
    ) -> None:
        event = DatabaseStatementEvent(
            topic=EVT_SQL(SQL_STATEMENT),
            connection_name="default",
            operation="query",
            duration_seconds=0.01,
            result="ok",
            result_count=1,
        )

        assert str(event.scope) == "sql.statement"
        assert event.connection_name == "default"
        assert event.operation == "query"
        assert event.result_count == 1
        assert not hasattr(event, "statement")

    @pytest.mark.anyio
    async def test_transaction_event_uses_sql_transaction_scope_and_outcome(
        self,
    ) -> None:
        event = DatabaseTransactionEvent(
            topic=EVT_SQL(TRANSACTION),
            connection_name="default",
            transaction_kind="transaction",
            outcome="commit",
        )

        assert str(event.scope) == "sql.transaction"
        assert event.outcome == "commit"

    @pytest.mark.anyio
    async def test_database_setup_and_statement_execution_publish_safe_events(
        self,
    ) -> None:
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site = await _start_events_site()
        site.require_capability(EventsCapability).subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:", modules=("wybra.sessions",)
        )
        try:
            with database.context:
                await database.connection().execute_query("select 1")
        finally:
            await close_database(database)
            await site.close()

        connection_event = next(
            event for event in observed if isinstance(event, DatabaseConnectionEvent)
        )
        statement_event = next(
            event for event in observed if isinstance(event, DatabaseStatementEvent)
        )
        assert str(connection_event.scope) == "sql.connection"
        assert connection_event.connection_name == "default"
        assert statement_event.operation == "query"
        assert statement_event.result == "ok"
        assert statement_event.result_count == 1
        assert not hasattr(statement_event, "statement")

    @pytest.mark.anyio
    async def test_transactions_savepoints_and_rollbacks_publish_outcomes(
        self,
    ) -> None:
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site = await _start_events_site()
        site.require_capability(EventsCapability).subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:", modules=("wybra.sessions",)
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
            await site.close()

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
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site = await _start_events_site()
        site.require_capability(EventsCapability).subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:", modules=("wybra.sessions",)
        )
        try:
            with database.context:
                with pytest.raises(asyncio.CancelledError):
                    async with in_transaction("default"):
                        raise asyncio.CancelledError()
        finally:
            await close_database(database)
            await site.close()

        transactions = [
            event for event in observed if isinstance(event, DatabaseTransactionEvent)
        ]
        assert [event.outcome for event in transactions] == ["begin", "rollback"]

    @pytest.mark.anyio
    async def test_failed_statement_event_preserves_the_database_error(self) -> None:
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site = await _start_events_site()
        site.require_capability(EventsCapability).subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:", modules=("wybra.sessions",)
        )
        try:
            with database.context:
                with pytest.raises(OperationalError):
                    await database.connection().execute_query("not valid sql")
        finally:
            await close_database(database)
            await site.close()

        statement_event = next(
            event for event in observed if isinstance(event, DatabaseStatementEvent)
        )
        assert statement_event.result == "error"
