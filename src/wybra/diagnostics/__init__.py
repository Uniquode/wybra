"""Runtime diagnostics API."""

from __future__ import annotations

from wybra.diagnostics.capabilities import (
    DiagnosticsCapability,
    DiagnosticSnapshot,
    DiagnosticsSubscription,
)
from wybra.diagnostics.context import (
    backend_operation_diagnostics,
    current_diagnostic_context,
    current_diagnostics,
    debug,
    diagnostic_context,
    diagnostic_operation,
    info,
    record_backend_operation,
    record_sql_query,
    record_template_render,
    request_diagnostics,
    template_render_diagnostics,
    trace,
)
from wybra.diagnostics.records import (
    DiagnosticContext,
    DiagnosticEvent,
    DiagnosticLevel,
    RequestDiagnostics,
    normalise_diagnostics_level,
)
from wybra.diagnostics.settings import DiagnosticsSettings

__all__ = (
    "DiagnosticEvent",
    "DiagnosticContext",
    "DiagnosticSnapshot",
    "DiagnosticLevel",
    "DiagnosticsSettings",
    "DiagnosticsCapability",
    "DiagnosticsSubscription",
    "RequestDiagnostics",
    "backend_operation_diagnostics",
    "current_diagnostics",
    "current_diagnostic_context",
    "debug",
    "diagnostic_context",
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
