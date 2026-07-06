"""Runtime diagnostics API."""

from __future__ import annotations

from wybra.diagnostics.context import (
    backend_operation_diagnostics,
    current_diagnostics,
    debug,
    diagnostic_operation,
    info,
    record_backend_operation,
    record_sql_query,
    record_template_render,
    request_diagnostics,
    template_render_diagnostics,
    trace,
)
from wybra.diagnostics.events import (
    DiagnosticEvent,
    DiagnosticLevel,
    RequestDiagnostics,
    normalise_diagnostics_level,
)
from wybra.diagnostics.settings import DiagnosticsSettings

__all__ = (
    "DiagnosticEvent",
    "DiagnosticLevel",
    "DiagnosticsSettings",
    "RequestDiagnostics",
    "backend_operation_diagnostics",
    "current_diagnostics",
    "debug",
    "diagnostic_operation",
    "info",
    "normalise_diagnostics_level",
    "record_backend_operation",
    "record_sql_query",
    "record_template_render",
    "request_diagnostics",
    "template_render_diagnostics",
    "trace",
)
