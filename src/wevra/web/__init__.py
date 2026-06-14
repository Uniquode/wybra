"""Reusable FastAPI, Starlette, Jinja, route, static, and form infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from wevra.site import Site
from wevra.site_config import app_config_from_site
from wevra.utils.paths import resolve_project_path
from wevra.web.config import module_config
from wevra.web.context import (
    resolve_context_providers,
    set_request_context,
    validate_context_providers,
)
from wevra.web.csrf import csrf_settings_from_config
from wevra.web.errors import ErrorHandlerOptions, register_error_handlers
from wevra.web.forms.csrf import CsrfProtector
from wevra.web.rendering import TemplateRenderer
from wevra.web.routes.contracts import API_PATH_PREFIX
from wevra.web.routes.discovery import (
    context_providers_from_modules,
    static_sources_from_modules,
    template_sources_from_modules,
)
from wevra.web.routes.registration import load_module_routes, register_module_routes
from wevra.web.security import SecurityHeaderOptions, register_security_headers
from wevra.web.staticfiles import static_app_from_config

TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE = (
    "wevra_web_template_context_middleware_registered"
)


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    static_mount_path = _normalise_static_mount_path(app_config.static.url_path)
    site.app.state.static_mount_path = static_mount_path

    csrf = getattr(site.app.state, "csrf", None)
    if csrf is None:
        csrf = csrf_settings_from_config(
            dict(site.config.get_config("app") or {}),
            dict(site.config.get_config("wevra.web") or {}),
        ).protector()
        site.app.state.csrf = csrf
    elif not isinstance(csrf, CsrfProtector):
        raise RuntimeError("CSRF protector is not configured correctly.")

    if not hasattr(site.app.state, "renderer"):
        site.app.state.renderer = TemplateRenderer(
            template_root=_template_root(
                app_config.project_root,
                app_config.templates.root,
            ),
            csrf=csrf,
            template_sources=template_sources_from_modules(site.modules),
            include_request_context=_request_context_enabled(site),
            auto_reload=app_config.templates.auto_reload,
            cache_size=app_config.templates.cache_size,
        )
    if not hasattr(site.app.state, "template_context_providers"):
        site.app.state.template_context_providers = validate_context_providers(
            context_providers_from_modules(site.modules)
        )

    _register_template_context_middleware(site)
    security_options = getattr(site.app.state, "security_header_options", None)
    register_security_headers(
        site.app,
        options=security_options
        if isinstance(security_options, SecurityHeaderOptions)
        else SecurityHeaderOptions(),
    )
    register_error_handlers(
        site.app,
        options=ErrorHandlerOptions(static_mount_path=static_mount_path),
    )
    site.app.mount(
        static_mount_path,
        static_app_from_config(
            project_root=app_config.project_root,
            static_root=app_config.static.root,
            static_sources=static_sources_from_modules(site.modules),
        ),
        name="static",
    )
    register_module_routes(
        site.app,
        load_module_routes(
            site.modules,
            route_prefixes=app_config.routes.prefixes,
        ),
    )


async def template_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if _should_resolve_template_context(request):
        providers = request.app.state.template_context_providers
        context = await resolve_context_providers(
            providers,
            request,
        )
        set_request_context(request, context)

    return await call_next(request)


def _register_template_context_middleware(site: Site) -> None:
    if getattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return

    site.app.middleware("http")(template_context_middleware)
    setattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, True)


def _should_resolve_template_context(request: Request) -> bool:
    path = request.url.path
    static_mount_path = getattr(request.app.state, "static_mount_path", "/static")
    return not (
        _matches_path_prefix(path, static_mount_path)
        or _matches_path_prefix(path, API_PATH_PREFIX)
    )


def _matches_path_prefix(path: str, prefix: str) -> bool:
    normalised_prefix = "/" + prefix.strip("/")
    return path == normalised_prefix or path.startswith(f"{normalised_prefix}/")


def _normalise_static_mount_path(url_path: str) -> str:
    return f"/{url_path.strip('/')}"


def _template_root(project_root: Path, template_root: Path | None) -> Path | None:
    return resolve_project_path(project_root, template_root)


def _request_context_enabled(site: Site) -> bool:
    web_config = site.config.get_config("wevra.web")
    if web_config is None:
        return True
    if not isinstance(web_config, Mapping):
        raise RuntimeError("Config value 'wevra.web' must be a mapping when set.")
    value = web_config.get("request_context_enabled")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    raise RuntimeError("Config value 'request_context_enabled' must be a boolean.")


__all__ = [
    "module_config",
    "setup_site",
    "template_context_middleware",
]
