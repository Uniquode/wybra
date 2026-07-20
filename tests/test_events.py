from __future__ import annotations

import pytest

from wybra.events import (
    EVT_SQL,
    MODEL,
    SQL_STATEMENT,
    EventScopeError,
    parse_event_scopes,
)


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
