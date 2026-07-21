from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

from wybra.diagnostics.capabilities import DiagnosticsCapability
from wybra.diagnostics.context import (
    REQUEST_DIAGNOSTICS_SCOPE_KEY,
    reset_current_diagnostic_context,
    reset_current_diagnostics,
    retain_completed_diagnostics,
    set_current_diagnostic_context,
    set_current_diagnostics,
)
from wybra.diagnostics.logging import emit_request_diagnostics
from wybra.diagnostics.records import DiagnosticContext, RequestDiagnostics
from wybra.diagnostics.settings import DiagnosticsSettings
from wybra.site import Site

DIAGNOSTICS_MIDDLEWARE_STATE_ATTRIBUTE = "wybra_diagnostics_middleware_registered"


def register_diagnostics_middleware(
    site: Site,
    settings: DiagnosticsSettings,
    capability: DiagnosticsCapability,
) -> None:
    if getattr(site.app.state, DIAGNOSTICS_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return
    setattr(site.app.state, DIAGNOSTICS_MIDDLEWARE_STATE_ATTRIBUTE, True)

    @site.app.middleware("http")
    async def diagnostics_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        context = DiagnosticContext(
            kind="request",
            description=f"{request.method} request",
        )
        diagnostics = RequestDiagnostics(
            method=request.method,
            path="",
            level=settings.level,
            slow_sql_threshold_seconds=settings.slow_sql_threshold_seconds,
            context=context,
        )
        request.scope[REQUEST_DIAGNOSTICS_SCOPE_KEY] = diagnostics
        context_token = set_current_diagnostic_context(context)
        token = set_current_diagnostics(diagnostics)
        started = time.perf_counter()
        status_code: int | None = None
        exception_type: str | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            exception_type = type(exc).__name__
            raise
        finally:
            diagnostics.finish(
                route_name=_route_name(request),
                status_code=status_code,
                exception_type=exception_type,
                duration_seconds=time.perf_counter() - started,
            )
            if settings.logging_bridge:
                emit_request_diagnostics(diagnostics)
            retain_completed_diagnostics(capability, diagnostics)
            reset_current_diagnostics(token)
            reset_current_diagnostic_context(context_token)


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    value = getattr(route, "name", None)
    return value if isinstance(value, str) and value else None


__all__ = (
    "DIAGNOSTICS_MIDDLEWARE_STATE_ATTRIBUTE",
    "register_diagnostics_middleware",
)
