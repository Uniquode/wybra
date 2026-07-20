"""Developer-facing class-based view support."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

from fastapi import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from wybra.api import ApiCapability, ApiPaging
from wybra.errors.diagnostics import structured_error, type_name
from wybra.events import (
    EVT_ROUTE,
    EVT_VIEW,
    EventsCapability,
    RouteDispatchedEvent,
    ViewCompletedEvent,
    publish_observation,
    scoped,
)
from wybra.site import SiteCapabilityError, get_site

type JsonValue = Mapping[str, Any] | Sequence[Any] | str | int | float | bool | None
HTTP_METHOD_HANDLERS = ("get", "post", "put", "patch", "delete", "head", "options")


@runtime_checkable
class _DeferredViewResponse(Protocol):
    """A view result that asynchronously resolves to an HTTP response."""

    async def render_response(self) -> Response: ...


@dataclass(frozen=True, slots=True)
class APIResult:
    data: Any
    paging: ApiPaging | None = None


type HandlerResult = Response | JsonValue | APIResult | _DeferredViewResponse
type ViewHandler = Callable[..., HandlerResult | Awaitable[HandlerResult]]


@dataclass(slots=True)
class View:
    """Base class for class-based HTTP endpoint views."""

    model: ClassVar[type[Any] | None] = None

    async def dispatch(self, request: Request, **kwargs: Any) -> Response:
        events = _events_for_request(request)
        route_name = _route_name(request)
        view_type = type_name(type(self))
        if events is not None:
            with scoped(EVT_ROUTE):
                await publish_observation(
                    events,
                    RouteDispatchedEvent(
                        method=request.method,
                        route_name=route_name,
                        view_type=view_type,
                    ),
                    message="view route dispatch event",
                )

        started = time.perf_counter()
        status_code: int | None = None
        error_type: str | None = None
        handler = self._handler_for(request.method)
        try:
            result = handler(request, **kwargs)
            if isawaitable(result):
                result = await result
            if isinstance(result, _DeferredViewResponse):
                response = await result.render_response()
            else:
                response = self.response_from_result(cast(HandlerResult, result))
            status_code = response.status_code
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            if events is not None:
                with scoped(EVT_VIEW):
                    await publish_observation(
                        events,
                        ViewCompletedEvent(
                            method=request.method,
                            route_name=route_name,
                            view_type=view_type,
                            duration_seconds=time.perf_counter() - started,
                            status_code=status_code,
                            error_type=error_type,
                        ),
                        message="view completion event",
                    )

    def _handler_for(self, method: str) -> ViewHandler:
        handler = getattr(self, method.lower(), None)
        if not callable(handler):
            return self.method_not_allowed
        return cast(ViewHandler, handler)

    def method_not_allowed(self, _request: Request, **_kwargs: Any) -> Response:
        return Response(
            status_code=405,
            headers={"Allow": ", ".join(self._allowed_methods())},
        )

    def _allowed_methods(self) -> tuple[str, ...]:
        return tuple(
            method.upper()
            for method in HTTP_METHOD_HANDLERS
            if callable(getattr(self, method, None))
        )

    def response_from_result(self, result: HandlerResult) -> Response:
        if isinstance(result, Response):
            return result
        if isinstance(result, str):
            return HTMLResponse(result)
        return JSONResponse(result)


def _events_for_request(request: Request) -> EventsCapability | None:
    """Return the core events capability when the request belongs to a site."""

    try:
        return get_site(request.app).optional_capability(EventsCapability)
    except SiteCapabilityError:
        return None


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    value = getattr(route, "name", None)
    return value if isinstance(value, str) and value else None


class HTMLView(View):
    """View base for literal HTML endpoint responses."""

    page_name: ClassVar[str | None] = None

    async def get(self, _request: Request, **_kwargs: Any) -> HTMLResponse:
        return HTMLResponse(self.get_page())

    def get_page(self) -> str:
        """Return the literal HTML page rendered by this view."""
        if self.page_name is None:
            raise ValueError("HTMLView requires page_name or an overridden get_page().")
        return self.page_name


@dataclass(slots=True)
class APIView(View):
    """View base for API endpoint responses."""

    api: ApiCapability | None = None

    def response_from_result(self, result: HandlerResult | APIResult) -> Response:
        if isinstance(result, Response):
            return result
        api = self._require_api_capability()
        if isinstance(result, APIResult):
            if result.paging is not None:
                return api.paged_response(result.data, paging=result.paging)
            return api.response(result.data)
        return api.response(result)

    def _require_api_capability(self) -> ApiCapability:
        if self.api is not None:
            return self.api
        raise SiteCapabilityError(
            structured_error(
                "Missing capability",
                capability_type=type_name(ApiCapability),
                provider_hint=(
                    "configure wybra.api or another ApiCapability provider, "
                    "or pass an ApiCapability to the APIView instance"
                ),
            )
        )


__all__ = [
    "APIView",
    "APIResult",
    "HTMLView",
    "HandlerResult",
    "JsonValue",
    "View",
]
