"""Developer-facing class-based view support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

from fastapi import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from tortoise.models import Model

from wybra.api import ApiCapability, ApiPaging
from wybra.errors.diagnostics import structured_error, type_name
from wybra.events import observe
from wybra.events.views import view_event
from wybra.scopes import ScopeAction, enforce_scope_access, get_scope_declaration
from wybra.site import SiteCapabilityError

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

    @observe(view_event)
    async def dispatch(self, request: Request, **kwargs: Any) -> Response:
        handler = self._handler_for(request.method)
        await self._enforce_scope_access(request, **kwargs)
        result = handler(request, **kwargs)
        if isawaitable(result):
            result = await result
        if isinstance(result, _DeferredViewResponse):
            return await result.render_response()
        return self.response_from_result(cast(HandlerResult, result))

    def scope_action(
        self,
        request: Request,
        **_kwargs: Any,
    ) -> ScopeAction | None:
        """Return the canonical action represented by this dispatch."""

        match request.method.upper():
            case "GET" | "HEAD":
                return ScopeAction.VIEW
            case "POST":
                return ScopeAction.CREATE
            case "PUT" | "PATCH":
                return ScopeAction.UPDATE
            case "DELETE":
                return ScopeAction.DELETE
            case _:
                return None

    async def _enforce_scope_access(self, request: Request, **kwargs: Any) -> None:
        if not callable(getattr(self, request.method.lower(), None)):
            return
        action = self.scope_action(request, **kwargs)
        if action is None:
            return
        view_type = type(self)
        model = self._scope_model()
        view_declaration = get_scope_declaration(view_type)
        model_declaration = get_scope_declaration(model) if model is not None else None
        if (
            not view_declaration.requires
            and not view_declaration.actions
            and (
                model_declaration is None
                or (not model_declaration.requires and not model_declaration.actions)
            )
        ):
            return
        await enforce_scope_access(
            request,
            target=view_type,
            action=action,
            model=model,
        )

    def _scope_model(self) -> type[Model] | None:
        model = type(self).model
        if isinstance(model, type) and issubclass(model, Model):
            return model
        return None

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
