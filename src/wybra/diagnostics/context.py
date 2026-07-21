from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from typing import cast

from fastapi import Request

from wybra.diagnostics.capabilities import (
    DiagnosticsCapability,
    process_diagnostics_capability,
)
from wybra.diagnostics.records import (
    DiagnosticContext,
    DiagnosticLevel,
    RequestDiagnostics,
)
from wybra.events._core import EventScope

REQUEST_DIAGNOSTICS_SCOPE_KEY = "wybra.diagnostics"
type DiagnosticAttributes = Mapping[str, object] | Callable[[], Mapping[str, object]]

logger = logging.getLogger(__name__)

_CURRENT_DIAGNOSTICS: ContextVar[RequestDiagnostics | None] = ContextVar(
    "wybra_current_diagnostics",
    default=None,
)
_CURRENT_CONTEXT: ContextVar[DiagnosticContext | None] = ContextVar(
    "wybra_current_diagnostic_context",
    default=None,
)


def current_diagnostics() -> RequestDiagnostics | None:
    return _CURRENT_DIAGNOSTICS.get()


def current_diagnostic_context() -> DiagnosticContext | None:
    """Return the active request, task, lifecycle, or explicit context."""

    return _CURRENT_CONTEXT.get()


def set_current_diagnostic_context(
    context: DiagnosticContext | None,
) -> Token[DiagnosticContext | None]:
    """Set the active context for framework request/lifecycle integration."""

    return _CURRENT_CONTEXT.set(context)


def reset_current_diagnostic_context(
    token: Token[DiagnosticContext | None],
) -> None:
    """Restore the prior active diagnostic context."""

    _CURRENT_CONTEXT.reset(token)


@asynccontextmanager
async def diagnostic_context(
    capability: DiagnosticsCapability,
    *,
    kind: str,
    description: str,
    level: DiagnosticLevel = "info",
) -> AsyncIterator[DiagnosticContext]:
    """Collect and retain explicitly scoped asynchronous diagnostic work."""

    parent = current_diagnostic_context()
    context = DiagnosticContext(
        kind=kind,
        description=description,
        parent_identifier=parent.identifier if parent is not None else None,
    )
    diagnostics = RequestDiagnostics(
        method="",
        path="",
        level=level,
        context=context,
    )
    context_token = _CURRENT_CONTEXT.set(context)
    diagnostics_token = set_current_diagnostics(diagnostics)
    started = time.perf_counter()
    exception_type: str | None = None
    try:
        yield context
    except Exception as exc:
        exception_type = type(exc).__name__
        raise
    finally:
        diagnostics.finish(
            route_name=None,
            status_code=None,
            exception_type=exception_type,
            duration_seconds=time.perf_counter() - started,
        )
        retain_completed_diagnostics(capability, diagnostics)
        reset_current_diagnostics(diagnostics_token)
        _CURRENT_CONTEXT.reset(context_token)


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
    def _record(diagnostics: RequestDiagnostics) -> None:
        diagnostics.record_event(
            level,
            category,
            name,
            attributes=_attributes(attributes),
            duration_seconds=duration_seconds,
            result=result,
        )

    _record_with_active_diagnostics(_record)


def record_topic(
    level: DiagnosticLevel,
    topic: EventScope,
    *,
    attributes: DiagnosticAttributes | None = None,
    duration_seconds: float | None = None,
    result: str | None = None,
) -> None:
    """Record a diagnostic observation from a validated event topic."""

    _record_with_active_diagnostics(
        lambda diagnostics: diagnostics.record_topic(
            level,
            topic,
            attributes=_attributes(attributes),
            duration_seconds=duration_seconds,
            result=result,
        )
    )


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
    operation: str | None = None,
    result_count: int | None = None,
    inserted_id: int | None = None,
) -> None:
    _record_with_active_diagnostics(
        lambda diagnostics: diagnostics.record_sql_query(
            statement,
            duration_seconds=duration_seconds,
            result=result,
            operation=operation,
            result_count=result_count,
            inserted_id=inserted_id,
        )
    )


def record_sql_operation(
    *,
    duration_seconds: float,
    result: str = "ok",
    operation: str | None = None,
    result_count: int | None = None,
    inserted_id: int | None = None,
    attributes: DiagnosticAttributes | None = None,
) -> None:
    """Record SQL metadata where statement text is intentionally unavailable."""

    _record_with_active_diagnostics(
        lambda diagnostics: diagnostics.record_sql_operation(
            duration_seconds=duration_seconds,
            result=result,
            operation=operation,
            result_count=result_count,
            inserted_id=inserted_id,
            attributes=dict(_attributes(attributes)),
        )
    )


def record_template_render(
    template_name: str,
    *,
    duration_seconds: float,
    result: str = "ok",
) -> None:
    _record_with_active_diagnostics(
        lambda diagnostics: diagnostics.record_template_render(
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
    def _record(diagnostics: RequestDiagnostics) -> None:
        diagnostics.record_backend_operation(
            category,
            name,
            attributes=_attributes(attributes),
            duration_seconds=duration_seconds,
            result=result,
            level=level,
        )

    _record_with_active_diagnostics(_record)


def retain_completed_diagnostics(
    capability: DiagnosticsCapability,
    diagnostics: RequestDiagnostics,
) -> None:
    """Retain diagnostics without allowing observation failures to affect work."""

    _record_safely(lambda: capability.record_completed(diagnostics))


def _record_with_active_diagnostics(
    record: Callable[[RequestDiagnostics], None],
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        _record_safely(lambda: record(diagnostics))
        return
    capability = process_diagnostics_capability()
    if capability is None:
        return
    standalone = RequestDiagnostics(method="", path="", level=capability.level)
    _record_safely(lambda: record(standalone))
    if capability.selects_diagnostics(standalone):
        retain_completed_diagnostics(capability, standalone)


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
    "current_diagnostic_context",
    "debug",
    "diagnostic_context",
    "diagnostic_operation",
    "info",
    "record_backend_operation",
    "record_event",
    "record_sql_query",
    "record_sql_operation",
    "record_template_render",
    "record_topic",
    "retain_completed_diagnostics",
    "reset_current_diagnostic_context",
    "request_diagnostics",
    "reset_current_diagnostics",
    "set_current_diagnostics",
    "set_current_diagnostic_context",
    "template_render_diagnostics",
    "trace",
)
