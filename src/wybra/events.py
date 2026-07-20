"""Typed, process-local event namespace values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final


class EventScopeError(ValueError):
    """Raised when an event scope or topic is invalid."""


@dataclass(frozen=True, slots=True)
class EventSegment:
    """A reusable validated segment in an event topic."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _normalise_segment(self.value))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class EventScope:
    """A validated dot-notation event selector or concrete topic."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise EventScopeError("Event scopes must contain at least one segment.")
        object.__setattr__(
            self,
            "segments",
            tuple(_normalise_segment(segment) for segment in self.segments),
        )

    def __call__(self, *segments: EventSegment | str) -> EventScope:
        """Create a child topic using reusable or dynamic validated segments."""

        return EventScope(
            self.segments
            + tuple(
                segment.value if isinstance(segment, EventSegment) else segment
                for segment in segments
            )
        )

    def matches(self, selector: EventScope) -> bool:
        """Return whether this concrete scope belongs to ``selector``."""

        return self.segments[: len(selector.segments)] == selector.segments

    def __str__(self) -> str:
        return ".".join(self.segments)


def event_segment(value: str) -> EventSegment:
    """Create a reusable event-topic segment."""

    return EventSegment(value)


def event_scope(value: str) -> EventScope:
    """Create a root event scope from dot-separated literal segments."""

    return EventScope(tuple(value.split(".")))


def parse_event_scopes(
    value: str | Iterable[str | EventScope],
) -> tuple[EventScope, ...]:
    """Parse configured selectors against Wybra's registered root scopes."""

    values = value.split(",") if isinstance(value, str) else value
    scopes = tuple(
        item if isinstance(item, EventScope) else event_scope(item.strip())
        for item in values
        if isinstance(item, EventScope) or item.strip()
    )
    if not scopes:
        raise EventScopeError("At least one event scope must be configured.")
    unknown_roots = sorted({scope.segments[0] for scope in scopes} - _ROOT_SCOPE_NAMES)
    if unknown_roots:
        raise EventScopeError(
            "Unknown event scope root: " + ", ".join(unknown_roots) + "."
        )
    return scopes


def _normalise_segment(value: str) -> str:
    segment = value.strip().lower()
    if not segment or segment == "*" or "." in segment:
        raise EventScopeError(
            "Event scope segments must be non-blank dot-free names; wildcards are "
            "not supported."
        )
    if not all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in segment
    ):
        raise EventScopeError(
            f"Event scope segment {value!r} must use ASCII letters, digits, or '_'."
        )
    return segment


SQL_STATEMENT: Final = event_segment("statement")
MODEL: Final = event_segment("model")
TEMPLATE_RENDER: Final = event_segment("render")
TRANSACTION: Final = event_segment("transaction")
SAVEPOINT: Final = event_segment("savepoint")
BEGIN: Final = event_segment("begin")
COMMIT: Final = event_segment("commit")
ROLLBACK: Final = event_segment("rollback")
RELEASE: Final = event_segment("release")
EVT_SQL: Final = event_scope("sql")
EVT_TEMPLATE: Final = event_scope("template")
EVT_CONTENT_TYPES: Final = event_scope("content_types")
DEFAULT_EVENT_SCOPES: Final = (EVT_SQL, EVT_TEMPLATE)
_ROOT_SCOPE_NAMES: Final = frozenset(
    scope.segments[0] for scope in (EVT_SQL, EVT_TEMPLATE, EVT_CONTENT_TYPES)
)
_SCOPE_DESCRIPTIONS: Final = {
    EVT_SQL: "Database statement and transaction diagnostics.",
    EVT_TEMPLATE: "Template rendering diagnostics.",
    EVT_CONTENT_TYPES: "Finalised content-type mappings.",
}


def available_event_scopes() -> tuple[tuple[EventScope, str], ...]:
    """Return the public root scopes and their developer-facing descriptions."""

    return tuple(_SCOPE_DESCRIPTIONS.items())


__all__ = (
    "EVT_CONTENT_TYPES",
    "DEFAULT_EVENT_SCOPES",
    "EVT_SQL",
    "EVT_TEMPLATE",
    "BEGIN",
    "COMMIT",
    "MODEL",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "SQL_STATEMENT",
    "TEMPLATE_RENDER",
    "TRANSACTION",
    "EventScope",
    "EventScopeError",
    "EventSegment",
    "available_event_scopes",
    "event_scope",
    "event_segment",
    "parse_event_scopes",
)
