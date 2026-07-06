from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from wybra.diagnostics.levels import (
    DIAGNOSTIC_LEVEL_VALUES,
    DiagnosticLevel,
    normalise_diagnostics_level,
)

SENSITIVE_ATTRIBUTE_PARTS: Final = (
    "authorisation",
    "authorization",
    "cookie",
    "credential",
    "csrf",
    "password",
    "secret",
    "session",
    "token",
)
MAX_STRING_ATTRIBUTE_LENGTH: Final = 500


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    level: DiagnosticLevel
    category: str
    name: str
    timestamp: float
    attributes: Mapping[str, object] = field(default_factory=dict)
    duration_seconds: float | None = None
    result: str | None = None

    def as_dict(self) -> dict[str, object]:
        values: dict[str, object] = {
            "level": self.level,
            "category": self.category,
            "name": self.name,
            "timestamp": self.timestamp,
            "attributes": dict(self.attributes),
        }
        if self.duration_seconds is not None:
            values["duration_seconds"] = self.duration_seconds
        if self.result is not None:
            values["result"] = self.result
        return values


@dataclass(slots=True)
class RequestDiagnostics:
    method: str
    path: str
    level: DiagnosticLevel = "info"
    slow_sql_threshold_seconds: float = 0.5
    route_name: str | None = None
    status_code: int | None = None
    exception_type: str | None = None
    duration_seconds: float | None = None
    events: list[DiagnosticEvent] = field(default_factory=list)
    sql_query_count: int = 0
    sql_total_duration_seconds: float = 0.0
    template_render_count: int = 0
    template_total_duration_seconds: float = 0.0
    backend_operation_count: int = 0

    def allows(self, level: DiagnosticLevel) -> bool:
        return DIAGNOSTIC_LEVEL_VALUES[level] >= DIAGNOSTIC_LEVEL_VALUES[self.level]

    def record_event(
        self,
        level: DiagnosticLevel,
        category: str,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        duration_seconds: float | None = None,
        result: str | None = None,
    ) -> None:
        if not self.allows(level):
            return
        self.events.append(
            DiagnosticEvent(
                level=level,
                category=category.strip() or "diagnostic",
                name=name.strip() or "event",
                timestamp=time.time(),
                attributes=_safe_attributes(attributes or {}),
                duration_seconds=_positive_duration(duration_seconds),
                result=_safe_result(result),
            )
        )

    def record_sql_query(
        self,
        statement: str,
        *,
        duration_seconds: float,
        result: str = "ok",
    ) -> None:
        duration = _positive_duration(duration_seconds) or 0.0
        self.sql_query_count += 1
        self.sql_total_duration_seconds += duration
        if self.allows("trace"):
            level: DiagnosticLevel | None = "trace"
        elif duration >= self.slow_sql_threshold_seconds:
            level = "info"
        else:
            level = None
        if level is not None:
            self.record_event(
                level,
                "sql",
                "query",
                attributes={"statement": _normalise_sql_statement(statement)},
                duration_seconds=duration,
                result=result,
            )

    def record_template_render(
        self,
        template_name: str,
        *,
        duration_seconds: float,
        result: str = "ok",
    ) -> None:
        duration = _positive_duration(duration_seconds) or 0.0
        self.template_render_count += 1
        self.template_total_duration_seconds += duration
        self.record_event(
            "trace",
            "template",
            "render",
            attributes={"template": template_name},
            duration_seconds=duration,
            result=result,
        )

    def record_backend_operation(
        self,
        category: str,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        duration_seconds: float | None = None,
        result: str | None = None,
        level: DiagnosticLevel = "debug",
    ) -> None:
        self.backend_operation_count += 1
        self.record_event(
            level,
            category,
            name,
            attributes=attributes,
            duration_seconds=duration_seconds,
            result=result,
        )

    def finish(
        self,
        *,
        route_name: str | None,
        status_code: int | None,
        exception_type: str | None,
        duration_seconds: float,
    ) -> None:
        self.route_name = route_name
        self.status_code = status_code
        self.exception_type = exception_type
        self.duration_seconds = _positive_duration(duration_seconds)
        self.record_event(
            "info",
            "request",
            "completed",
            attributes={
                "method": self.method,
                "path": self.path,
                "route": route_name,
                "status_code": status_code,
                "exception_type": exception_type,
            },
            duration_seconds=self.duration_seconds,
            result="error" if exception_type else "ok",
        )

    def summary(self) -> dict[str, object]:
        event_counts = Counter(event.category for event in self.events)
        return {
            "method": self.method,
            "path": self.path,
            "route": self.route_name,
            "status_code": self.status_code,
            "exception_type": self.exception_type,
            "duration_seconds": self.duration_seconds,
            "event_counts": dict(event_counts),
            "sql_query_count": self.sql_query_count,
            "sql_total_duration_seconds": self.sql_total_duration_seconds,
            "template_render_count": self.template_render_count,
            "template_total_duration_seconds": self.template_total_duration_seconds,
            "backend_operation_count": self.backend_operation_count,
        }


def _safe_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in attributes.items():
        key_name = str(key)
        if _is_sensitive_attribute_name(key_name):
            safe[key_name] = "[redacted]"
        else:
            safe[key_name] = _safe_value(value)
    return safe


def _is_sensitive_attribute_name(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_ATTRIBUTE_PARTS)


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_STRING_ATTRIBUTE_LENGTH:
            return value
        return f"{value[:MAX_STRING_ATTRIBUTE_LENGTH]}..."
    return f"<{type(value).__name__}>"


def _safe_result(result: str | None) -> str | None:
    if result is None:
        return None
    return str(_safe_value(result))


def _positive_duration(duration_seconds: float | None) -> float | None:
    if duration_seconds is None:
        return None
    return max(0.0, float(duration_seconds))


def _normalise_sql_statement(statement: str) -> str:
    return " ".join(statement.split())


__all__ = (
    "DIAGNOSTIC_LEVEL_VALUES",
    "DiagnosticEvent",
    "DiagnosticLevel",
    "RequestDiagnostics",
    "normalise_diagnostics_level",
)
