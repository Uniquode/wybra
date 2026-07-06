from __future__ import annotations

import time
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.ext.asyncio import AsyncEngine

from wybra.diagnostics.context import current_diagnostics, record_sql_query

SQL_DIAGNOSTICS_START_ATTRIBUTE = "_wybra_diagnostics_started_at"


def instrument_sqlalchemy_engine(engine: AsyncEngine | Engine) -> None:
    target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if event.contains(target, "before_cursor_execute", _before_cursor_execute):
        return
    event.listen(target, "before_cursor_execute", _before_cursor_execute)
    event.listen(target, "after_cursor_execute", _after_cursor_execute)


def _before_cursor_execute(
    _conn: Any,
    _cursor: Any,
    _statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    if current_diagnostics() is not None:
        setattr(context, SQL_DIAGNOSTICS_START_ATTRIBUTE, time.perf_counter())


def _after_cursor_execute(
    _conn: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    started = getattr(context, SQL_DIAGNOSTICS_START_ATTRIBUTE, None)
    if isinstance(started, float):
        record_sql_query(
            statement,
            duration_seconds=time.perf_counter() - started,
        )


__all__ = (
    "SQL_DIAGNOSTICS_START_ATTRIBUTE",
    "_after_cursor_execute",
    "_before_cursor_execute",
    "instrument_sqlalchemy_engine",
)
