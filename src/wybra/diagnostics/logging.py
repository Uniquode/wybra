from __future__ import annotations

import logging

from wybra.core.logging import TRACE_LEVEL, get_logger
from wybra.diagnostics.events import (
    DiagnosticEvent,
    DiagnosticLevel,
    RequestDiagnostics,
)

DIAGNOSTICS_LOGGER_NAME = "wybra.diagnostics"


def emit_request_diagnostics(diagnostics: RequestDiagnostics) -> None:
    logger = get_logger(DIAGNOSTICS_LOGGER_NAME)
    for event in diagnostics.events:
        emit_diagnostic_event(event, logger=logger)
    logger.info(
        "diagnostic request summary",
        extra={
            "wybra_diagnostics": {
                "kind": "request_summary",
                **diagnostics.summary(),
            }
        },
    )


def emit_diagnostic_event(
    event: DiagnosticEvent,
    *,
    logger: logging.Logger | None = None,
) -> None:
    diagnostic_logger = logger or get_logger(DIAGNOSTICS_LOGGER_NAME)
    diagnostic_logger.log(
        _logging_level(event.level),
        "diagnostic event category=%s name=%s",
        event.category,
        event.name,
        extra={"wybra_diagnostics": {"kind": "event", **event.as_dict()}},
    )


def _logging_level(level: DiagnosticLevel) -> int:
    if level == "trace":
        return TRACE_LEVEL
    if level == "debug":
        return logging.DEBUG
    return logging.INFO


__all__ = (
    "DIAGNOSTICS_LOGGER_NAME",
    "emit_diagnostic_event",
    "emit_request_diagnostics",
)
