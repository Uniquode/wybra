"""Reusable FastAPI, Starlette, Jinja, route, and form infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from wybra.assets import StaticAssetCapability
from wybra.site import Site, SiteCapabilityProxy, get_site
from wybra.site_config import app_config_from_site
from wybra.utils.paths import resolve_project_path
from wybra.web.config import WebSettings, module_config
from wybra.web.context import (
    resolve_context_providers,
    set_request_context,
    validate_context_providers,
)
from wybra.web.csrf import CsrfSettings
from wybra.web.errors import ErrorHandlerOptions, register_error_handlers
from wybra.web.forms.csrf import CsrfProtector
from wybra.web.rendering import TemplateRenderer
from wybra.web.routes.contracts import API_PATH_PREFIX
from wybra.web.routes.discovery import (
    context_providers_from_modules,
    template_sources_from_modules,
)
from wybra.web.routes.registration import load_module_routes, register_module_routes
from wybra.web.security import SecurityHeaderOptions, register_security_headers

TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE = (
    "wybra_web_template_context_middleware_registered"
)


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    asset_capability = site.capability_proxy(StaticAssetCapability)

    csrf = getattr(site.app.state, "csrf", None)
    if csrf is None:
        csrf = CsrfSettings.load_settings(site.config).protector()
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
        options=ErrorHandlerOptions(
            static_mount_path=lambda: _optional_static_mount_path(asset_capability)
        ),
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
    static_mount_path = _optional_static_mount_path(
        get_site(request.app).capability_proxy(StaticAssetCapability)
    )
    is_static_path = static_mount_path is not None and _matches_path_prefix(
        path,
        static_mount_path,
    )
    return not (is_static_path or _matches_path_prefix(path, API_PATH_PREFIX))


def _matches_path_prefix(path: str, prefix: str) -> bool:
    normalised_prefix = "/" + prefix.strip("/")
    return path == normalised_prefix or path.startswith(f"{normalised_prefix}/")


def _template_root(project_root: Path, template_root: Path | None) -> Path | None:
    return resolve_project_path(project_root, template_root)


def _request_context_enabled(site: Site) -> bool:
    return WebSettings.load_settings(site.config).request_context_enabled


def _optional_static_mount_path(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> str | None:
    capability = proxy.optional()
    if capability is None:
        return None
    return _normalise_static_mount_path(capability.url_path)


def _normalise_static_mount_path(url_path: str) -> str:
    return "/" + url_path.strip("/")


__all__ = [
    "module_config",
    "setup_site",
    "template_context_middleware",
]
