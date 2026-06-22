"""Developer-facing class-based view support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, ClassVar, Protocol, cast

from fastapi import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

JsonValue = Mapping[str, Any] | Sequence[Any] | str | int | float | bool | None
HandlerResult = Response | JsonValue
ViewHandler = Callable[..., HandlerResult | Awaitable[HandlerResult]]
HTTP_METHOD_HANDLERS = ("get", "post", "put", "patch", "delete", "head", "options")


@dataclass(slots=True)
class View:
    """Base class for class-based HTTP endpoint views."""

    model: ClassVar[type[Any] | None] = None

    async def dispatch(self, request: Request, **kwargs: Any) -> Response:
        handler = self._handler_for(request.method)
        result = handler(request, **kwargs)
        if isawaitable(result):
            result = await result
        return self.response_from_result(cast(HandlerResult, result))

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


class HTMLView(View):
    """View base for literal HTML endpoint responses."""

    def response_from_result(self, result: HandlerResult) -> Response:
        if isinstance(result, Response):
            return result
        if isinstance(result, str):
            return HTMLResponse(result)
        return HTMLResponse(str(result))


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    size: int
    total: int | None = None


@dataclass(frozen=True, slots=True)
class APIResult:
    data: Any
    page: Page | None = None


class APIResponseFormatter(Protocol):
    """Future API capability integration point for final response formatting."""

    def response_from_result(self, result: HandlerResult | APIResult) -> Response: ...


@dataclass(slots=True)
class APIView(View):
    """View base for API endpoint responses."""

    formatter: APIResponseFormatter | None = None

    def response_from_result(self, result: HandlerResult | APIResult) -> Response:
        if self.formatter is not None:
            return self.formatter.response_from_result(result)
        if isinstance(result, Response):
            return result
        if isinstance(result, APIResult):
            payload: dict[str, Any] = {"data": result.data}
            if result.page is not None:
                payload["page"] = {
                    "number": result.page.number,
                    "size": result.page.size,
                    "total": result.page.total,
                }
            return JSONResponse(payload)
        return JSONResponse(result)


__all__ = [
    "APIResponseFormatter",
    "APIView",
    "APIResult",
    "HTMLView",
    "HandlerResult",
    "JsonValue",
    "Page",
    "View",
]
