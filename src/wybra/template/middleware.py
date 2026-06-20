from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

from wybra.assets import StaticAssetCapability
from wybra.config.transforms import to_url_path
from wybra.core.url_paths import matches_path_prefix
from wybra.site import Site, SiteCapabilityProxy, get_site
from wybra.template.context import (
    resolve_context_providers,
    set_request_context,
    validate_context_providers,
)
from wybra.template.discovery import context_providers_from_modules

TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE = (
    "wybra_template_context_middleware_registered"
)
TEMPLATE_CONTEXT_PROVIDERS_STATE_ATTRIBUTE = "wybra_template_context_providers"
API_PATH_PREFIX = "/api"


def register_template_context_middleware(site: Site) -> None:
    if getattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return

    if not hasattr(site.app.state, TEMPLATE_CONTEXT_PROVIDERS_STATE_ATTRIBUTE):
        setattr(
            site.app.state,
            TEMPLATE_CONTEXT_PROVIDERS_STATE_ATTRIBUTE,
            validate_context_providers(context_providers_from_modules(site.modules)),
        )

    site.app.middleware("http")(template_context_middleware)
    setattr(site.app.state, TEMPLATE_CONTEXT_MIDDLEWARE_STATE_ATTRIBUTE, True)


async def template_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if _should_resolve_template_context(request):
        providers = getattr(
            request.app.state,
            TEMPLATE_CONTEXT_PROVIDERS_STATE_ATTRIBUTE,
        )
        context = await resolve_context_providers(providers, request)
        set_request_context(request, context)

    return await call_next(request)


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
    return matches_path_prefix(path, prefix)


def _optional_static_mount_path(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> str | None:
    capability = proxy.optional()
    if capability is None:
        return None
    return to_url_path(capability.url_path, name="StaticAssetCapability.url_path")


__all__ = (
    "register_template_context_middleware",
    "template_context_middleware",
)
