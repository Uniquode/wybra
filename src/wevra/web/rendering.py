from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, select_autoescape

from wevra.core.resources import PackageResourceSource
from wevra.web.context import TemplateContext, get_request_context
from wevra.web.forms.csrf import CsrfProtector
from wevra.web.templating import build_template_loader


@dataclass(slots=True)
class TemplateRenderer:
    template_root: Path | None = None
    csrf: CsrfProtector | None = None
    template_sources: tuple[PackageResourceSource, ...] = ()
    include_request_context: bool = True
    auto_reload: bool | None = None
    cache_size: int = 400
    environment: Environment = field(init=False)

    def __post_init__(self) -> None:
        loader = build_template_loader(
            template_root=self.template_root,
            template_sources=self.template_sources,
        )
        environment_options: dict[str, Any] = {}
        if self.auto_reload is not None:
            environment_options["auto_reload"] = self.auto_reload

        self.environment = Environment(
            loader=loader,
            autoescape=select_autoescape(("html", "xml")),
            cache_size=self.cache_size,
            **environment_options,
        )

    def render_template(self, template_name: str, context: dict[str, Any]) -> str:
        return self.environment.get_template(template_name).render(context)

    @staticmethod
    def _resolve_route_name(request: Request) -> str:
        route = request.scope.get("route")
        route_name = getattr(route, "name", None)
        if isinstance(route_name, str):
            return route_name

        return "unknown"

    def _template_context(
        self,
        request: Request,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        framework_context: dict[str, Any] = {
            "route_name": self._resolve_route_name(request),
            "static_mount_path": self._resolve_static_mount_path(request),
        }
        if self.include_request_context:
            framework_context["request"] = request
        if self.csrf is not None:
            framework_context.update(self.csrf.token_context(request))

        return (
            TemplateContext.from_mapping(framework_context)
            .merge(context)
            .merge(get_request_context(request))
            .as_dict()
        )

    @staticmethod
    def _resolve_static_mount_path(request: Request) -> str:
        try:
            app_state = request.app.state
        except (AttributeError, KeyError, RuntimeError):
            return "/static"

        static_mount_path = getattr(app_state, "static_mount_path", None)
        if isinstance(static_mount_path, str) and static_mount_path.strip():
            return static_mount_path.rstrip("/")

        settings = getattr(app_state, "settings", None)
        settings_static_mount_path = getattr(settings, "static_mount_path", None)
        if (
            isinstance(settings_static_mount_path, str)
            and settings_static_mount_path.strip()
        ):
            return settings_static_mount_path.rstrip("/")

        return "/static"

    def render_page(
        self,
        template_name: str,
        request: Request,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        template_context = self._template_context(request, context)
        response = HTMLResponse(
            self.render_template(
                template_name,
                template_context,
            ),
            status_code=status_code,
        )
        if self.csrf is not None:
            self.csrf.set_cookie(request, response)

        return response

    def render_partial(
        self,
        template_name: str,
        request: Request,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return self.render_page(
            template_name, request, context, status_code=status_code
        )


def renderer_from(request: Request) -> TemplateRenderer:
    renderer = getattr(request.app.state, "renderer", None)
    if not isinstance(renderer, TemplateRenderer):
        raise RuntimeError("Template renderer is not configured on the application.")

    return renderer


def render_page(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return renderer_from(request).render_page(
        template_name,
        request,
        context or {},
        status_code=status_code,
    )


def render_partial(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return renderer_from(request).render_partial(
        template_name,
        request,
        context or {},
        status_code=status_code,
    )
