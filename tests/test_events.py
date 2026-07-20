from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from wybra.events import (
    BEGIN,
    EVT_SQL,
    EVT_TEMPLATE,
    MODEL,
    SQL_STATEMENT,
    TRANSACTION,
    Event,
    EventScopeError,
    current_scope,
    extend,
    parse_event_scopes,
    scope,
)


@dataclass(frozen=True, slots=True)
class TransactionStarted(Event):
    kind: ClassVar = BEGIN
    transaction_id: str


@dataclass(frozen=True, slots=True)
class MissingKind(Event):
    identifier: str


def test_event_scope_creates_a_chainable_topic() -> None:
    topic = EVT_SQL(SQL_STATEMENT, MODEL, "article")

    assert str(topic) == "sql.statement.model.article"
    assert topic.matches(EVT_SQL)
    assert not EVT_SQL.matches(topic)


@pytest.mark.parametrize("value", ("sql.*", "sql..statement", "unknown"))
def test_event_scope_parser_rejects_invalid_or_unknown_selectors(value: str) -> None:
    with pytest.raises(EventScopeError):
        parse_event_scopes(value)


def test_event_scope_parser_accepts_a_comma_separated_selector_list() -> None:
    assert tuple(map(str, parse_event_scopes("sql, template.render"))) == (
        "sql",
        "template.render",
    )


@pytest.mark.anyio
async def test_scope_decorator_propagates_and_extends_through_async_calls() -> None:
    @extend(TRANSACTION)
    async def nested() -> str:
        return str(current_scope())

    @scope(EVT_SQL)
    async def scoped() -> str:
        return await nested()

    assert await scoped() == "sql.transaction"
    assert current_scope() is None


@pytest.mark.anyio
async def test_scope_decorator_overrides_and_restores_the_parent_scope() -> None:
    @scope(EVT_TEMPLATE)
    async def overridden() -> str:
        return str(current_scope())

    @scope(EVT_SQL)
    async def scoped() -> tuple[str, str]:
        before = str(current_scope())
        during = await overridden()
        return before, f"{during}:{current_scope()}"

    assert await scoped() == ("sql", "template:sql")
    assert current_scope() is None


@pytest.mark.anyio
async def test_scope_decorator_restores_the_prior_scope_after_an_exception() -> None:
    @extend(TRANSACTION)
    async def failing() -> None:
        assert current_scope() == EVT_SQL(TRANSACTION)
        raise RuntimeError("expected")

    @scope(EVT_SQL)
    async def scoped() -> None:
        with pytest.raises(RuntimeError, match="expected"):
            await failing()
        assert current_scope() == EVT_SQL

    await scoped()
    assert current_scope() is None


@pytest.mark.anyio
async def test_event_snapshots_the_resolved_scope_and_occurrence_time() -> None:
    @extend(TRANSACTION)
    async def create_event() -> TransactionStarted:
        return TransactionStarted(transaction_id="transaction-1")

    @scope(EVT_SQL)
    async def scoped() -> TransactionStarted:
        return await create_event()

    event = await scoped()

    assert event.scope == EVT_SQL(TRANSACTION, BEGIN)
    assert event.occurred_at > 0


def test_event_requires_an_active_scope() -> None:
    with pytest.raises(EventScopeError, match="active scope"):
        TransactionStarted(transaction_id="transaction-1")


@pytest.mark.anyio
async def test_event_requires_a_declared_kind() -> None:
    @scope(EVT_SQL)
    async def create_event() -> None:
        with pytest.raises(EventScopeError, match="must declare"):
            MissingKind(identifier="missing-kind")

    await create_event()
