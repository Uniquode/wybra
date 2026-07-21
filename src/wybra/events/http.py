"""HTTP event contracts, descriptors, and correlation helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from inspect import BoundArguments, iscoroutinefunction
from typing import ClassVar
from uuid import uuid7

from fastapi import Request
from fastapi.responses import Response

from wybra.events._core import (
    _CURRENT_CONTEXT,
    COMPLETED,
    EVT_REQUEST,
    STARTED,
    Event,
    EventContext,
    EventOutcome,
    EventSegment,
)


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


def request_context[**P, T](
    function: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    """Decorate an async HTTP boundary with a fresh UUIDv7 context."""

    if not iscoroutinefunction(function):
        raise TypeError("Request contexts require async callables.")

    @wraps(function)
    async def contextual(*args: P.args, **kwargs: P.kwargs) -> T:
        token = _CURRENT_CONTEXT.set(EventContext(request_id=uuid7()))
        try:
            return await function(*args, **kwargs)
        finally:
            _CURRENT_CONTEXT.reset(token)

    return contextual


def request_event(
    call: BoundArguments,
    outcome: EventOutcome | None,
) -> Event:
    """Build a safe request lifecycle event without exposing request content."""

    request = call.arguments["request"]
    if not isinstance(request, Request):
        raise TypeError("Request events require a FastAPI request.")
    if outcome is None:
        return RequestStartedEvent(topic=EVT_REQUEST(STARTED), method=request.method)

    response = outcome.result
    status_code = response.status_code if isinstance(response, Response) else None
    return RequestCompletedEvent(
        topic=EVT_REQUEST(COMPLETED),
        method=request.method,
        duration_seconds=outcome.duration_seconds,
        status_code=status_code,
        route_name=_route_name(request),
        error_type=outcome.error_type,
    )


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    value = getattr(route, "name", None)
    return value if isinstance(value, str) and value else None


__all__ = ("request_context", "request_event")
