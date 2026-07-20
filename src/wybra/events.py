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
from asyncio import current_task, ensure_future, shield
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction
from threading import Lock
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Final,
    ParamSpec,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from wybra.config import BaseSettings, ConfigDef, to_bool
from wybra.events_config import EVENTS_CONFIG_DEF, EVENTS_CONFIG_SECTION

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
class EventsSettings(BaseSettings):
    """Core event-delivery settings loaded before configured modules."""

    module_config: ClassVar[ConfigDef] = EVENTS_CONFIG_DEF
    config_section: ClassVar[str | None] = EVENTS_CONFIG_SECTION

    enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", to_bool(self.enabled))


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


class _DisabledEventsCapability:
    """No-op core sink used when optional event delivery is disabled."""

    def subscribe(self, selector: EventScope, handler: EventHandler) -> None:
        del selector, handler
        return None

    async def publish(self, event: Event) -> None:
        del event
        return None

    def enabled(self) -> bool:
        """Return whether delivery can accept observations."""

        return False


@dataclass(slots=True)
class _LazyEventsCapability:
    """Resolve the configured site-local event sink on first use."""

    site: Site
    _capability: EventsCapability | None = field(default=None, init=False)
    _resolve_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def subscribe(self, selector: EventScope, handler: EventHandler) -> None:
        self._resolve().subscribe(selector, handler)

    async def publish(self, event: Event) -> None:
        await self._resolve().publish(event)

    def enabled(self) -> bool:
        """Return whether the resolved sink is accepting observations."""

        return event_delivery_enabled(self._resolve())

    def _resolve(self) -> EventsCapability:
        capability = self._capability
        if capability is None:
            # ``subscribe()`` remains synchronous, so use a thread lock rather
            # than an asyncio primitive to make first resolution safe for both
            # event-loop tasks and synchronous callers on other threads.
            with self._resolve_lock:
                capability = self._capability
                if capability is None:
                    settings = EventsSettings.load_settings(self.site.config)
                    capability = (
                        EventDispatcher()
                        if settings.enabled
                        else _DisabledEventsCapability()
                    )
                    self._capability = capability
        return capability


@dataclass(slots=True)
class EventDispatcher:
    """Dispatch events sequentially to handlers selected by scope prefix."""

    _handlers: list[tuple[EventScope, EventHandler]] = field(default_factory=list)

    def enabled(self) -> bool:
        """Return whether this concrete dispatcher accepts observations."""

        return True

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
        handler_task = ensure_future(handler(event))
        try:
            await shield(handler_task)
        except BaseException as exc:
            if _external_cancellation_requested():
                handler_task.cancel()
                raise
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


def setup_core_events(site: Site) -> None:
    """Register lazy core event delivery before configured module setup."""

    site.provide_capability(EventsCapability, _LazyEventsCapability(site))


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

    await publish_observation(dispatcher, before, message="pre-operation event")
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
    except BaseException:
        if _external_cancellation_requested():
            raise
        logger.exception("Unable to publish %s", message)


async def _publish_outcome_safely(
    dispatcher: EventsCapability,
    outcome_event: OutcomeEventFactory,
    outcome: EventOutcome,
) -> None:
    try:
        event = outcome_event(outcome)
    except BaseException:
        logger.exception("Unable to create post-operation event")
        return
    await publish_observation(dispatcher, event, message="post-operation event")


async def publish_observation(
    dispatcher: EventsCapability,
    event: Event,
    *,
    message: str,
) -> None:
    """Publish an event without allowing delivery to alter an operation."""

    await _publish_safely(dispatcher, event, message=message)


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


@contextmanager
def scoped(value: EventScope) -> Iterator[None]:
    """Temporarily establish an event scope for the current async context."""

    token = _CURRENT_SCOPE.set(value)
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


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
    error: BaseException | None = None,
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
CONNECTION: Final = event_segment("connection")
MODEL: Final = event_segment("model")
TEMPLATE_RENDER: Final = event_segment("render")
CACHE_READ: Final = event_segment("read")
CACHE_SET: Final = event_segment("set")
CACHE_DELETE: Final = event_segment("delete")
CACHE_FILL: Final = event_segment("fill")
FORM: Final = event_segment("form")
VALIDATION: Final = event_segment("validation")
PERSISTENCE: Final = event_segment("persistence")
ACCOUNT: Final = event_segment("account")
CREDENTIAL: Final = event_segment("credential")
SESSION: Final = event_segment("session")
SECURITY: Final = event_segment("security")
POLICY: Final = event_segment("policy")
CSRF: Final = event_segment("csrf")
TRANSACTION: Final = event_segment("transaction")
SAVEPOINT: Final = event_segment("savepoint")
MODULE: Final = event_segment("module")
CAPABILITY: Final = event_segment("capability")
REQUEST: Final = event_segment("request")
ROUTE: Final = event_segment("route")
VIEW: Final = event_segment("view")
GENERIC: Final = event_segment("generic")
COLLECTION: Final = event_segment("collection")
ITEM: Final = event_segment("item")
BULK: Final = event_segment("bulk")
SETUP: Final = event_segment("setup")
POST_SETUP: Final = event_segment("post_setup")
STARTUP: Final = event_segment("startup")
SHUTDOWN: Final = event_segment("shutdown")
STARTED: Final = event_segment("started")
COMPLETED: Final = event_segment("completed")
RESOLVED: Final = event_segment("resolved")
UNAVAILABLE: Final = event_segment("unavailable")
DISPATCH: Final = event_segment("dispatch")
HANDLER: Final = event_segment("handler")
ERRORS: Final = event_segment("errors")
SUCCEEDED: Final = event_segment("succeeded")
FAILED: Final = event_segment("failed")
DENIED: Final = event_segment("denied")
BEGIN: Final = event_segment("begin")
COMMIT: Final = event_segment("commit")
ROLLBACK: Final = event_segment("rollback")
RELEASE: Final = event_segment("release")
EVT_SQL: Final = event_scope("sql")
EVT_TEMPLATE: Final = event_scope("template")
EVT_CACHE: Final = event_scope("cache")
EVT_FORM: Final = event_scope("form")
EVT_ACCOUNT: Final = event_scope("account")
EVT_CREDENTIAL: Final = event_scope("credential")
EVT_SESSION: Final = event_scope("session")
EVT_SECURITY: Final = event_scope("security")
EVT_CONTENT_TYPES: Final = event_scope("content_types")
EVT_EVENTS: Final = event_scope("events")
EVT_EVENTS_ERRORS: Final = EVT_EVENTS(ERRORS)
EVT_SITE: Final = event_scope("site")
EVT_REQUEST: Final = event_scope("request")
EVT_ROUTE: Final = event_scope("route")
EVT_VIEW: Final = event_scope("view")
DEFAULT_EVENT_SCOPES: Final = (EVT_SQL, EVT_TEMPLATE, EVT_EVENTS_ERRORS)
_ROOT_SCOPE_NAMES: Final = frozenset(
    scope.segments[0]
    for scope in (
        EVT_SQL,
        EVT_TEMPLATE,
        EVT_CACHE,
        EVT_FORM,
        EVT_ACCOUNT,
        EVT_CREDENTIAL,
        EVT_SESSION,
        EVT_SECURITY,
        EVT_CONTENT_TYPES,
        EVT_EVENTS,
        EVT_SITE,
        EVT_REQUEST,
        EVT_ROUTE,
        EVT_VIEW,
    )
)
_SCOPE_DESCRIPTIONS: Final = {
    EVT_SQL: "Database statement and transaction diagnostics.",
    EVT_TEMPLATE: "Template rendering diagnostics.",
    EVT_CACHE: "Cache operation observations.",
    EVT_FORM: "Form validation and persistence observations.",
    EVT_ACCOUNT: "Account lifecycle observations.",
    EVT_CREDENTIAL: "Credential and access-control observations.",
    EVT_SESSION: "Session lifecycle observations without session data.",
    EVT_SECURITY: "Security-policy and denial observations.",
    EVT_CONTENT_TYPES: "Finalised content-type mappings.",
    EVT_EVENTS: "Application event dispatch and handler outcomes.",
    EVT_EVENTS_ERRORS: "Application event-handler failures.",
    EVT_SITE: "Site and configured-module lifecycle observations.",
    EVT_REQUEST: "HTTP request lifecycle observations.",
    EVT_ROUTE: "Resolved HTTP route dispatch observations.",
    EVT_VIEW: "Class-based view execution observations.",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ModuleSetupEvent(Event):
    """An observation of a configured module's ``setup_site`` hook."""

    kind: ClassVar[EventSegment] = SETUP
    module: str
    outcome: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModulePostSetupEvent(Event):
    """An observation of a configured module's ``post_setup_site`` hook."""

    kind: ClassVar[EventSegment] = POST_SETUP
    module: str
    outcome: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestStartedEvent(Event):
    """An observation at the outer Wybra HTTP middleware entry."""

    kind: ClassVar[EventSegment] = STARTED
    method: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestCompletedEvent(Event):
    """An observation after an HTTP request returns or raises."""

    kind: ClassVar[EventSegment] = COMPLETED
    method: str
    duration_seconds: float
    status_code: int | None
    route_name: str | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteDispatchedEvent(Event):
    """An observation of a resolved route entering a class-based view."""

    kind: ClassVar[EventSegment] = DISPATCH
    method: str
    route_name: str | None
    view_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewCompletedEvent(Event):
    """An observation of a class-based view dispatch outcome."""

    kind: ClassVar[EventSegment] = COMPLETED
    method: str
    route_name: str | None
    view_type: str
    duration_seconds: float
    status_code: int | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GenericViewCompletedEvent(Event):
    """An observation of a model-driven collection, item, or bulk operation."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    model_type: str | None
    content_type: str | None
    duration_seconds: float
    status_code: int | None
    affected_count: int | None = None
    skipped_count: int | None = None
    failed_count: int | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateRenderCompletedEvent(Event):
    """An observation of one asynchronous template-rendering outcome."""

    kind: ClassVar[EventSegment] = COMPLETED
    template_name: str
    duration_seconds: float
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheOperationCompletedEvent(Event):
    """An observation of a completed cache operation without its raw key."""

    kind: ClassVar[EventSegment] = COMPLETED
    owner: str
    key_fingerprint: str
    outcome: str
    duration_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheOperationFailedEvent(Event):
    """An observation of a failed cache operation without its raw key."""

    kind: ClassVar[EventSegment] = FAILED
    owner: str
    key_fingerprint: str
    operation: str
    duration_seconds: float
    error_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FormValidationCompletedEvent(Event):
    """An observation of a form validation attempt without submitted values."""

    kind: ClassVar[EventSegment] = COMPLETED
    form_type: str
    field_count: int
    invalid_field_count: int
    valid: bool
    duration_seconds: float
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FormPersistenceCompletedEvent(Event):
    """An observation of a form persistence outcome without model values."""

    kind: ClassVar[EventSegment] = COMPLETED
    form_type: str
    model_types: tuple[str, ...]
    operation: str
    changed_fields: tuple[str, ...]
    affected_count: int
    created: bool
    updated: bool
    deleted: bool
    stale_conflict: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class FormPersistenceFailedEvent(Event):
    """An observation of a failed form persistence operation."""

    kind: ClassVar[EventSegment] = FAILED
    form_type: str
    model_types: tuple[str, ...]
    operation: str
    duration_seconds: float
    error_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountLifecycleEvent(Event):
    """An account lifecycle outcome with opaque identity metadata only."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    outcome: str
    user_id: str | None = None
    masked_email: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialAccessEvent(Event):
    """A credential/access-control outcome without credential material."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    provider: str
    outcome: str
    user_id: str | None = None
    masked_email: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionLifecycleEvent(Event):
    """A session lifecycle outcome without session identifiers or payloads."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    backend: str
    outcome: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityPolicyEvent(Event):
    """A safe outcome for one configured security policy."""

    kind: ClassVar[EventSegment] = COMPLETED
    policy: str
    outcome: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityDenialEvent(Event):
    """A secret-free observation of a security rejection."""

    kind: ClassVar[EventSegment] = DENIED
    mechanism: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityResolvedEvent(Event):
    """An observation of a proxy resolving a site capability."""

    kind: ClassVar[EventSegment] = RESOLVED
    capability_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityUnavailableEvent(Event):
    """An observation of a proxy not finding a site capability."""

    kind: ClassVar[EventSegment] = UNAVAILABLE
    capability_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityProvidedEvent(Event):
    """An observation that a capability was registered with a site."""

    kind: ClassVar[EventSegment] = COMPLETED
    capability_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteLifecycleEvent(Event):
    """A site startup or shutdown outcome."""

    kind: ClassVar[EventSegment] = COMPLETED
    phase: str
    error_count: int = 0


def available_event_scopes() -> tuple[tuple[EventScope, str], ...]:
    """Return public event selectors and their developer-facing descriptions."""

    return tuple(_SCOPE_DESCRIPTIONS.items())


def event_delivery_enabled(dispatcher: EventsCapability | None) -> bool:
    """Return whether an optional dispatcher will retain observations.

    Test and application-provided dispatchers that predate the optional
    ``enabled()`` fast path remain treated as enabled.
    """

    if dispatcher is None:
        return False
    enabled = getattr(dispatcher, "enabled", None)
    if not callable(enabled):
        return True
    try:
        return bool(enabled())
    except BaseException:
        logger.exception("Unable to determine whether event delivery is enabled")
        return False


def _external_cancellation_requested() -> bool:
    """Return whether cancellation was requested for the producer task itself."""

    task = current_task()
    return task is not None and task.cancelling() > 0


__all__ = (
    "EVT_CONTENT_TYPES",
    "EVT_EVENTS",
    "EVT_EVENTS_ERRORS",
    "EVT_REQUEST",
    "EVT_CACHE",
    "EVT_FORM",
    "EVT_ACCOUNT",
    "EVT_CREDENTIAL",
    "EVT_SESSION",
    "EVT_SECURITY",
    "EVT_ROUTE",
    "EVT_SITE",
    "DEFAULT_EVENT_SCOPES",
    "event_delivery_enabled",
    "EVT_SQL",
    "EVT_TEMPLATE",
    "EVT_VIEW",
    "BEGIN",
    "CACHE_DELETE",
    "CACHE_FILL",
    "CACHE_READ",
    "CACHE_SET",
    "ACCOUNT",
    "CREDENTIAL",
    "SESSION",
    "SECURITY",
    "POLICY",
    "CSRF",
    "FORM",
    "BULK",
    "COLLECTION",
    "COMMIT",
    "CONNECTION",
    "current_scope",
    "MODEL",
    "MODULE",
    "CAPABILITY",
    "REQUEST",
    "ROUTE",
    "SETUP",
    "POST_SETUP",
    "STARTUP",
    "SHUTDOWN",
    "PERSISTENCE",
    "STARTED",
    "COMPLETED",
    "RESOLVED",
    "UNAVAILABLE",
    "DISPATCH",
    "ERRORS",
    "FAILED",
    "DENIED",
    "HANDLER",
    "ITEM",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "SQL_STATEMENT",
    "TEMPLATE_RENDER",
    "TemplateRenderCompletedEvent",
    "TRANSACTION",
    "VALIDATION",
    "SUCCEEDED",
    "VIEW",
    "EventScope",
    "EventScopeError",
    "EventSegment",
    "EventsSettings",
    "Event",
    "GENERIC",
    "GenericViewCompletedEvent",
    "FormPersistenceCompletedEvent",
    "FormPersistenceFailedEvent",
    "FormValidationCompletedEvent",
    "ModuleSetupEvent",
    "ModulePostSetupEvent",
    "RequestStartedEvent",
    "RequestCompletedEvent",
    "RouteDispatchedEvent",
    "ViewCompletedEvent",
    "CapabilityResolvedEvent",
    "CapabilityUnavailableEvent",
    "CapabilityProvidedEvent",
    "SiteLifecycleEvent",
    "CacheOperationCompletedEvent",
    "CacheOperationFailedEvent",
    "AccountLifecycleEvent",
    "CredentialAccessEvent",
    "SessionLifecycleEvent",
    "SecurityPolicyEvent",
    "SecurityDenialEvent",
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
    "publish_observation",
    "scope",
    "scoped",
    "setup_core_events",
)
