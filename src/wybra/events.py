"""Typed observational events with contextual scopes and async dispatch.

Routine dispatch diagnostics are opt-in. Handler failures are recorded through
the dedicated ``events.errors`` scope. Operational exception logging preserves
the original exception traceback, so event handlers must not raise exceptions
whose messages contain secrets. When routine telemetry is selected outside a
diagnostic context, each observation is retained as an individual snapshot.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Final,
    ParamSpec,
    Protocol,
    TypeVar,
    runtime_checkable,
)

if TYPE_CHECKING:
    from wybra.site import Site

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """An immutable observation classified by its current event scope."""

    kind: ClassVar[EventSegment]
    scope: EventScope = field(init=False)
    occurred_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        parent = current_scope()
        if parent is None:
            raise EventScopeError("Published events require an active scope.")
        kind = getattr(type(self), "kind", None)
        if not isinstance(kind, EventSegment):
            raise EventScopeError(
                f"Event type {type(self).__name__} must declare an EventSegment kind."
            )
        object.__setattr__(self, "scope", parent(kind))


type EventHandler = Callable[[Event], Awaitable[None]]
type OutcomeEventFactory = Callable[[EventOutcome], Event]


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """Secret-safe completion state supplied to a post-operation event factory."""

    succeeded: bool
    error_type: str | None = None


@runtime_checkable
class EventsCapability(Protocol):
    """Process-local delivery of typed observational events."""

    def subscribe(self, selector: EventScope, handler: EventHandler) -> None: ...

    async def publish(self, event: Event) -> None: ...


@dataclass(slots=True)
class EventDispatcher:
    """Dispatch events sequentially to handlers selected by scope prefix."""

    _handlers: list[tuple[EventScope, EventHandler]] = field(default_factory=list)

    def subscribe(self, selector: EventScope, handler: EventHandler) -> None:
        if not iscoroutinefunction(handler) and not iscoroutinefunction(
            handler.__call__
        ):
            raise TypeError("Event handlers must be async callables.")
        self._handlers.append((selector, handler))

    async def publish(self, event: Event) -> None:
        dispatch_started = time.perf_counter()
        handler_failed = False
        for selector, handler in self._handlers:
            if event.scope.matches(selector):
                handler_failed = (
                    await self._dispatch_handler(event, handler) or handler_failed
                )
        _record_dispatch_diagnostic(
            event,
            duration_seconds=time.perf_counter() - dispatch_started,
            handler_failed=handler_failed,
        )

    async def _dispatch_handler(self, event: Event, handler: EventHandler) -> bool:
        started = time.perf_counter()
        try:
            await handler(event)
        except Exception as exc:
            logger.exception(
                "Event handler failed",
                extra={
                    "event_type": _callable_identity(type(event)),
                    "event_scope": str(event.scope),
                    "handler": _callable_identity(handler),
                    "error_type": type(exc).__name__,
                },
            )
            _record_handler_diagnostic(
                event,
                handler,
                duration_seconds=time.perf_counter() - started,
                error=exc,
            )
            return True
        else:
            _record_handler_diagnostic(
                event,
                handler,
                duration_seconds=time.perf_counter() - started,
            )
            return False


async def setup_site(site: Site) -> None:
    """Provide the site-local event delivery capability."""

    site.provide_capability(EventsCapability, EventDispatcher())


@asynccontextmanager
async def observe_operation(
    dispatcher: EventsCapability,
    before: Event,
    outcome_event: OutcomeEventFactory,
) -> AsyncIterator[None]:
    """Publish non-controlling observations around one operation.

    The operation's own result or exception always wins.  Post-operation
    observation construction and dispatch are independently isolated so that
    observation code cannot alter the business operation it describes.
    """

    await _publish_safely(dispatcher, before, message="pre-operation event")
    try:
        yield
    except Exception as exc:
        await _publish_outcome_safely(
            dispatcher,
            outcome_event,
            EventOutcome(succeeded=False, error_type=type(exc).__name__),
        )
        raise
    else:
        await _publish_outcome_safely(
            dispatcher,
            outcome_event,
            EventOutcome(succeeded=True),
        )


async def _publish_safely(
    dispatcher: EventsCapability,
    event: Event,
    *,
    message: str,
) -> None:
    try:
        await dispatcher.publish(event)
    except Exception:
        logger.exception("Unable to publish %s", message)


async def _publish_outcome_safely(
    dispatcher: EventsCapability,
    outcome_event: OutcomeEventFactory,
    outcome: EventOutcome,
) -> None:
    try:
        event = outcome_event(outcome)
    except Exception:
        logger.exception("Unable to create post-operation event")
        return
    await _publish_safely(dispatcher, event, message="post-operation event")


_CURRENT_SCOPE: ContextVar[EventScope | None] = ContextVar(
    "wybra_current_event_scope",
    default=None,
)


def current_scope() -> EventScope | None:
    """Return the current async event scope, if one has been established."""

    return _CURRENT_SCOPE.get()


def scope(
    value: EventScope,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorate an async callable with an overriding event scope."""

    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def scoped(*args: P.args, **kwargs: P.kwargs) -> T:
            token = _CURRENT_SCOPE.set(value)
            try:
                return await function(*args, **kwargs)
            finally:
                _CURRENT_SCOPE.reset(token)

        return scoped

    return decorate


def extend(
    *segments: EventSegment | str,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorate an async callable with a scope extending its caller's scope."""

    if not segments:
        raise EventScopeError("Event scope extensions require at least one segment.")

    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def extended(*args: P.args, **kwargs: P.kwargs) -> T:
            parent = current_scope()
            if parent is None:
                raise EventScopeError("Event scope extensions require an active scope.")
            token = _CURRENT_SCOPE.set(parent(*segments))
            try:
                return await function(*args, **kwargs)
            finally:
                _CURRENT_SCOPE.reset(token)

        return extended

    return decorate


def event_segment(value: str) -> EventSegment:
    """Create a reusable event-topic segment."""

    return EventSegment(value)


def _callable_identity(value: object) -> str:
    """Return a stable diagnostic identity without inspecting payload values."""

    value_type = type(value)
    module = getattr(value, "__module__", value_type.__module__)
    qualname = getattr(value, "__qualname__", value_type.__qualname__)
    return f"{module}.{qualname}"


def _record_dispatch_diagnostic(
    event: Event,
    *,
    duration_seconds: float,
    handler_failed: bool,
) -> None:
    try:
        # Diagnostics owns its context API and imports scope values from this
        # module, so defer this optional observer import to avoid a cycle.
        from wybra.diagnostics.context import record_topic

        record_topic(
            "trace",
            EVT_EVENTS(DISPATCH),
            attributes={
                "event_type": _callable_identity(type(event)),
                "event_scope": str(event.scope),
            },
            duration_seconds=duration_seconds,
            result="error" if handler_failed else "ok",
        )
    except Exception:
        logger.exception("Unable to record event dispatch diagnostics")


def _record_handler_diagnostic(
    event: Event,
    handler: EventHandler,
    *,
    duration_seconds: float,
    error: Exception | None = None,
) -> None:
    try:
        # See _record_dispatch_diagnostic() for the deferred observer import.
        from wybra.diagnostics.context import record_topic

        attributes: dict[str, str] = {
            "event_type": _callable_identity(type(event)),
            "event_scope": str(event.scope),
            "handler": _callable_identity(handler),
        }
        if error is not None:
            attributes["error_type"] = type(error).__name__
        failure = error is not None
        record_topic(
            "info" if failure else "debug",
            (
                EVT_EVENTS_ERRORS(HANDLER, FAILED)
                if failure
                else EVT_EVENTS(HANDLER, SUCCEEDED)
            ),
            attributes=attributes,
            duration_seconds=duration_seconds,
            result="error" if failure else "ok",
        )
    except Exception:
        logger.exception("Unable to record event handler diagnostics")


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
DISPATCH: Final = event_segment("dispatch")
HANDLER: Final = event_segment("handler")
ERRORS: Final = event_segment("errors")
SUCCEEDED: Final = event_segment("succeeded")
FAILED: Final = event_segment("failed")
BEGIN: Final = event_segment("begin")
COMMIT: Final = event_segment("commit")
ROLLBACK: Final = event_segment("rollback")
RELEASE: Final = event_segment("release")
EVT_SQL: Final = event_scope("sql")
EVT_TEMPLATE: Final = event_scope("template")
EVT_CONTENT_TYPES: Final = event_scope("content_types")
EVT_EVENTS: Final = event_scope("events")
EVT_EVENTS_ERRORS: Final = EVT_EVENTS(ERRORS)
DEFAULT_EVENT_SCOPES: Final = (EVT_SQL, EVT_TEMPLATE, EVT_EVENTS_ERRORS)
_ROOT_SCOPE_NAMES: Final = frozenset(
    scope.segments[0]
    for scope in (EVT_SQL, EVT_TEMPLATE, EVT_CONTENT_TYPES, EVT_EVENTS)
)
_SCOPE_DESCRIPTIONS: Final = {
    EVT_SQL: "Database statement and transaction diagnostics.",
    EVT_TEMPLATE: "Template rendering diagnostics.",
    EVT_CONTENT_TYPES: "Finalised content-type mappings.",
    EVT_EVENTS: "Application event dispatch and handler outcomes.",
    EVT_EVENTS_ERRORS: "Application event-handler failures.",
}


def available_event_scopes() -> tuple[tuple[EventScope, str], ...]:
    """Return the public root scopes and their developer-facing descriptions."""

    return tuple(_SCOPE_DESCRIPTIONS.items())


__all__ = (
    "EVT_CONTENT_TYPES",
    "EVT_EVENTS",
    "EVT_EVENTS_ERRORS",
    "DEFAULT_EVENT_SCOPES",
    "EVT_SQL",
    "EVT_TEMPLATE",
    "BEGIN",
    "COMMIT",
    "current_scope",
    "MODEL",
    "DISPATCH",
    "ERRORS",
    "FAILED",
    "HANDLER",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "SQL_STATEMENT",
    "TEMPLATE_RENDER",
    "TRANSACTION",
    "SUCCEEDED",
    "EventScope",
    "EventScopeError",
    "EventSegment",
    "Event",
    "EventDispatcher",
    "EventHandler",
    "EventOutcome",
    "EventsCapability",
    "OutcomeEventFactory",
    "available_event_scopes",
    "event_scope",
    "event_segment",
    "extend",
    "parse_event_scopes",
    "observe_operation",
    "scope",
    "setup_site",
)
