from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, select_autoescape

from web_core.context import get_request_context
from web_core.csrf import CsrfProtector
from web_core.resources import PackageResourceSource
from web_core.templates import build_template_loader

RESERVED_TEMPLATE_CONTEXT_KEYS = frozenset(
    {
        "request",
        "route_name",
        "csrf_field_name",
        "csrf_header_name",
        "csrf_token",
        "static_mount_path",
    }
)


@dataclass(slots=True)
class TemplateRenderer:
    template_root: Path | None = None
    csrf: CsrfProtector | None = None
    template_sources: tuple[PackageResourceSource, ...] = ()
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
        base_context: dict[str, Any] = {
            "request": request,
            "route_name": self._resolve_route_name(request),
            "static_mount_path": self._resolve_static_mount_path(request),
        }
        csrf_context = self.csrf.token_context(request) if self.csrf is not None else {}
        internal_keys = base_context.keys() | csrf_context.keys()
        provider_context = get_request_context(request)
        self._reject_internal_context_overrides(internal_keys, provider_context)
        self._reject_internal_context_overrides(internal_keys, context)

        return base_context | csrf_context | provider_context | context

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

    @staticmethod
    def _reject_internal_context_overrides(
        internal_keys: set[str],
        context: dict[str, Any],
    ) -> None:
        overlapping_keys = internal_keys & context.keys()
        if overlapping_keys:
            keys = ", ".join(sorted(overlapping_keys))
            raise ValueError(f"Template context overrides internal keys: {keys}")

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
