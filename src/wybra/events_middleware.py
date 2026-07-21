"""Outer HTTP lifecycle observations for the core events service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Request
from fastapi.responses import Response

from wybra.events import observe
from wybra.events.http import request_context, request_event
from wybra.site import Site

EVENTS_MIDDLEWARE_STATE_ATTRIBUTE: Final = "wybra_events_middleware_registered"


def register_event_lifecycle_middleware(site: Site) -> None:
    """Register the outermost Wybra HTTP observation boundary once."""

    if getattr(site.app.state, EVENTS_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return
    setattr(site.app.state, EVENTS_MIDDLEWARE_STATE_ATTRIBUTE, True)

    @site.app.middleware("http")
    @request_context
    @observe(request_event)
    async def event_lifecycle_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        return await call_next(request)


__all__ = (
    "EVENTS_MIDDLEWARE_STATE_ATTRIBUTE",
    "register_event_lifecycle_middleware",
)
