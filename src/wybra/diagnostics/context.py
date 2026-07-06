from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from typing import cast

from fastapi import Request

from wybra.diagnostics.events import DiagnosticLevel, RequestDiagnostics

REQUEST_DIAGNOSTICS_SCOPE_KEY = "wybra.diagnostics"
type DiagnosticAttributes = Mapping[str, object] | Callable[[], Mapping[str, object]]

logger = logging.getLogger(__name__)

_CURRENT_DIAGNOSTICS: ContextVar[RequestDiagnostics | None] = ContextVar(
    "wybra_current_diagnostics",
    default=None,
)


def current_diagnostics() -> RequestDiagnostics | None:
    return _CURRENT_DIAGNOSTICS.get()


def set_current_diagnostics(
    diagnostics: RequestDiagnostics | None,
) -> Token[RequestDiagnostics | None]:
    return _CURRENT_DIAGNOSTICS.set(diagnostics)


def reset_current_diagnostics(token: Token[RequestDiagnostics | None]) -> None:
    _CURRENT_DIAGNOSTICS.reset(token)


def request_diagnostics(request: Request) -> RequestDiagnostics | None:
    diagnostics = request.scope.get(REQUEST_DIAGNOSTICS_SCOPE_KEY)
    return diagnostics if isinstance(diagnostics, RequestDiagnostics) else None


def info(
    category: str,
    name: str,
    *,
    attributes: DiagnosticAttributes | None = None,
    duration_seconds: float | None = None,
    result: str | None = None,
) -> None:
    record_event(
        "info",
        category,
        name,
        attributes=attributes,
        duration_seconds=duration_seconds,
        result=result,
    )


def debug(
    category: str,
    name: str,
    *,
    attributes: DiagnosticAttributes | None = None,
    duration_seconds: float | None = None,
    result: str | None = None,
) -> None:
    record_event(
        "debug",
        category,
        name,
        attributes=attributes,
        duration_seconds=duration_seconds,
        result=result,
    )


def trace(
    category: str,
    name: str,
    *,
    attributes: DiagnosticAttributes | None = None,
    duration_seconds: float | None = None,
    result: str | None = None,
) -> None:
    record_event(
        "trace",
        category,
        name,
        attributes=attributes,
        duration_seconds=duration_seconds,
        result=result,
    )


def record_event(
    level: DiagnosticLevel,
    category: str,
    name: str,
    *,
    attributes: DiagnosticAttributes | None = None,
    duration_seconds: float | None = None,
    result: str | None = None,
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is None or not diagnostics.allows(level):
        return

    def _record() -> None:
        diagnostics.record_event(
            level,
            category,
            name,
            attributes=_attributes(attributes),
            duration_seconds=duration_seconds,
            result=result,
        )

    _record_safely(_record)


@contextmanager
def template_render_diagnostics(template_name: str) -> Iterator[None]:
    with diagnostic_operation(
        lambda duration_seconds, result: record_template_render(
            template_name,
            duration_seconds=duration_seconds,
            result=result,
        )
    ):
        yield


@asynccontextmanager
async def backend_operation_diagnostics(
    category: str,
    name: str,
    *,
    attributes: DiagnosticAttributes | None = None,
    level: DiagnosticLevel = "debug",
) -> AsyncIterator[None]:
    with diagnostic_operation(
        lambda duration_seconds, result: record_backend_operation(
            category,
            name,
            duration_seconds=duration_seconds,
            result=result,
            attributes=attributes,
            level=level,
        )
    ):
        yield


@contextmanager
def diagnostic_operation(
    record: Callable[[float, str], None],
) -> Iterator[None]:
    started = time.perf_counter()
    result = "ok"
    try:
        yield
    except Exception:
        result = "error"
        raise
    finally:
        record(time.perf_counter() - started, result)


def _record_safely(record: Callable[[], None]) -> None:
    try:
        record()
    except Exception:
        logger.debug(
            "Diagnostics recording failed; application behaviour is preserved.",
            exc_info=True,
        )


def record_sql_query(
    statement: str,
    *,
    duration_seconds: float,
    result: str = "ok",
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        _record_safely(
            lambda: diagnostics.record_sql_query(
                statement,
                duration_seconds=duration_seconds,
                result=result,
            )
        )


def record_template_render(
    template_name: str,
    *,
    duration_seconds: float,
    result: str = "ok",
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        _record_safely(
            lambda: diagnostics.record_template_render(
                template_name,
                duration_seconds=duration_seconds,
                result=result,
            )
        )


def record_backend_operation(
    category: str,
    name: str,
    *,
    duration_seconds: float | None = None,
    result: str | None = None,
    attributes: DiagnosticAttributes | None = None,
    level: DiagnosticLevel = "debug",
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is None:
        return

    def _record() -> None:
        diagnostics.record_backend_operation(
            category,
            name,
            attributes=_attributes(attributes),
            duration_seconds=duration_seconds,
            result=result,
            level=level,
        )

    _record_safely(_record)


def _attributes(
    attributes: DiagnosticAttributes | None,
) -> Mapping[str, object]:
    if attributes is None:
        return {}
    if isinstance(attributes, Mapping):
        return cast(Mapping[str, object], attributes)
    return attributes()


__all__ = (
    "DiagnosticAttributes",
    "REQUEST_DIAGNOSTICS_SCOPE_KEY",
    "backend_operation_diagnostics",
    "current_diagnostics",
    "debug",
    "diagnostic_operation",
    "info",
    "record_backend_operation",
    "record_event",
    "record_sql_query",
    "record_template_render",
    "request_diagnostics",
    "reset_current_diagnostics",
    "set_current_diagnostics",
    "template_render_diagnostics",
    "trace",
)
