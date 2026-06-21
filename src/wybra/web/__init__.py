"""Reusable FastAPI, Starlette, route, and form infrastructure."""

from __future__ import annotations

from wybra.assets import StaticAssetCapability
from wybra.config.transforms import to_url_path
from wybra.site import Site, SiteCapabilityProxy
from wybra.site_config import app_config_from_site
from wybra.template import TemplateCapability
from wybra.web.config import module_config
from wybra.web.errors import ErrorHandlerOptions, register_error_handlers
from wybra.web.routes.inspection import inspect_route_tree
from wybra.web.routes.registration import load_module_routes, register_module_routes
from wybra.web.security import SecurityHeaderOptions, register_security_headers


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    asset_capability = site.capability_proxy(StaticAssetCapability)

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


async def post_setup_site(site: Site) -> None:
    if any(
        route.shape.template is not None
        for route in inspect_route_tree(site.app).routes
    ):
        site.require_capability(TemplateCapability)


def _optional_static_mount_path(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> str | None:
    capability = proxy.optional()
    if capability is None:
        return None
    return to_url_path(capability.url_path, name="StaticAssetCapability.url_path")


__all__ = [
    "module_config",
    "post_setup_site",
    "setup_site",
]
