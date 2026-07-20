"""Outer HTTP lifecycle observations for the core events service."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Request
from fastapi.responses import Response

from wybra.events import (
    EVT_REQUEST,
    EventsCapability,
    RequestCompletedEvent,
    RequestStartedEvent,
    publish_observation,
    scoped,
)
from wybra.site import Site

EVENTS_MIDDLEWARE_STATE_ATTRIBUTE: Final = "wybra_events_middleware_registered"


def register_event_lifecycle_middleware(site: Site) -> None:
    """Register the outermost Wybra HTTP observation boundary once."""

    if getattr(site.app.state, EVENTS_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return
    setattr(site.app.state, EVENTS_MIDDLEWARE_STATE_ATTRIBUTE, True)
    events = site.require_capability(EventsCapability)

    @site.app.middleware("http")
    async def event_lifecycle_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        status_code: int | None = None
        error_type: str | None = None
        with scoped(EVT_REQUEST):
            await publish_observation(
                events,
                RequestStartedEvent(method=request.method),
                message="request start event",
            )
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                await publish_observation(
                    events,
                    RequestCompletedEvent(
                        method=request.method,
                        duration_seconds=time.perf_counter() - started,
                        status_code=status_code,
                        route_name=_route_name(request),
                        error_type=error_type,
                    ),
                    message="request completion event",
                )


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    value = getattr(route, "name", None)
    return value if isinstance(value, str) and value else None


__all__ = (
    "EVENTS_MIDDLEWARE_STATE_ATTRIBUTE",
    "register_event_lifecycle_middleware",
)
