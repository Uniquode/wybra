from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from wevra.web.forms.csrf import CsrfProtector
from wevra.web.forms.security import is_safe_method
from wevra.web.rendering import TemplateRenderer
from wevra.web.routes.contracts import PARTIAL_PATH_PREFIX
from wevra.web.routes.registration import (
    HtmlRouteDefinition,
    HtmlView,
)


@dataclass(slots=True)
class HtmlDispatcher:
    renderer: TemplateRenderer
    csrf: CsrfProtector | None = None
    _routes: list[HtmlRouteDefinition] = field(default_factory=list)

    def register(self, definitions: Iterable[HtmlRouteDefinition]) -> None:
        self._routes.extend(definitions)

    def select_view(self, route_name: str, method: str) -> HtmlView:
        for definition in self._routes:
            if definition.name == route_name and method in definition.methods:
                return definition.view

        raise LookupError(f"Unknown HTML route: {route_name} [{method}]")

    async def dispatch(self, route_name: str, request: Request) -> Response:
        if not is_safe_method(request.method) and self.csrf is not None:
            if not await self.csrf.validate_request(request):
                raise HTTPException(status_code=403, detail="Invalid CSRF token.")

        return await self.select_view(route_name, request.method).render(
            request, self.renderer
        )


def _build_endpoint(dispatcher: HtmlDispatcher, route_name: str):
    async def endpoint(request: Request) -> Response:
        return await dispatcher.dispatch(route_name, request)

    return endpoint


def register_html_routes(
    app: FastAPI,
    dispatcher: HtmlDispatcher,
    definitions: Iterable[HtmlRouteDefinition],
) -> None:
    route_definitions = tuple(definitions)
    dispatcher.register(route_definitions)
    for definition in route_definitions:
        is_partial_path = definition.path.startswith(f"{PARTIAL_PATH_PREFIX}/")
        if definition.surface == "partial" and not is_partial_path:
            raise ValueError(
                f"Partial HTML routes must live under {PARTIAL_PATH_PREFIX}/: "
                f"{definition.path}"
            )
        if definition.surface == "page" and is_partial_path:
            raise ValueError(
                f"Page HTML routes cannot live under {PARTIAL_PATH_PREFIX}/: "
                f"{definition.path}"
            )
        app.add_api_route(
            definition.path,
            _build_endpoint(dispatcher, definition.name),
            methods=list(definition.methods),
            include_in_schema=False,
            name=definition.name,
        )
