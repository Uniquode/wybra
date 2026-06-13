"""Reusable FastAPI, Starlette, Jinja, route, static, and form infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response
from starlette.types import ASGIApp

from wevra.site import Site
from wevra.site_config import app_config_from_site
from wevra.web.context import (
    resolve_context_providers,
    set_request_context,
    validate_context_providers,
)
from wevra.web.errors import ErrorHandlerOptions, register_error_handlers
from wevra.web.forms.csrf import CsrfProtector
from wevra.web.rendering import RESERVED_TEMPLATE_CONTEXT_KEYS, TemplateRenderer
from wevra.web.routes.contracts import API_PATH_PREFIX
from wevra.web.routes.discovery import (
    context_providers_from_modules,
    static_sources_from_modules,
    template_sources_from_modules,
)
from wevra.web.routes.registration import load_module_routes, register_module_routes
from wevra.web.security import SecurityHeaderOptions, register_security_headers
from wevra.web.staticfiles import ComposedStaticFiles, NoStaticFiles

AUTH_MODULE_NAME = "wevra.auth"
HOST_ROUTE_MODULES_STATE_ATTRIBUTE = "wevra_web_host_route_modules"
TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE = (
    "wevra_web_template_context_middleware_registered"
)


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    static_mount_path = _normalise_static_mount_path(app_config.static.url_path)
    site.app.state.static_mount_path = static_mount_path

    csrf = getattr(site.app.state, "csrf", None)
    if csrf is not None and not isinstance(csrf, CsrfProtector):
        raise RuntimeError("CSRF protector is not configured correctly.")

    if not hasattr(site.app.state, "renderer"):
        site.app.state.renderer = TemplateRenderer(
            template_root=getattr(site.app.state, "template_root", None),
            csrf=csrf,
            template_sources=template_sources_from_modules(site.modules),
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
        _static_app(site),
        name="static",
    )
    register_module_routes(
        site.app,
        load_module_routes(
            _modules_registered_by_web(site),
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
            reserved_keys=RESERVED_TEMPLATE_CONTEXT_KEYS,
        )
        set_request_context(request, context)

    return await call_next(request)


def _register_template_context_middleware(site: Site) -> None:
    if getattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return

    site.app.middleware("http")(template_context_middleware)
    setattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, True)


def _static_app(site: Site) -> ASGIApp:
    configured_static_app = getattr(site.app.state, "static_app", None)
    if configured_static_app is not None:
        return configured_static_app

    static_sources = static_sources_from_modules(site.modules)
    if static_sources:
        return ComposedStaticFiles(static_sources)

    return NoStaticFiles()


def _modules_registered_by_web(site: Site) -> tuple[str, ...]:
    host_modules = getattr(site.app.state, HOST_ROUTE_MODULES_STATE_ATTRIBUTE, ())
    excluded_modules = {AUTH_MODULE_NAME}
    if isinstance(host_modules, tuple) and all(
        isinstance(module, str) for module in host_modules
    ):
        excluded_modules.update(host_modules)
    return tuple(module for module in site.modules if module not in excluded_modules)


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


__all__ = [
    "setup_site",
    "template_context_middleware",
]
