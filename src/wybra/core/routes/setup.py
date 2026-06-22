from __future__ import annotations

from typing import TYPE_CHECKING

from wybra.site_config import app_config_from_site

from .registration import load_module_routes, register_module_routes

if TYPE_CHECKING:
    from wybra.site import Site


def register_configured_routes_for_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    register_module_routes(
        site.app,
        load_module_routes(
            site.modules,
            route_prefixes=app_config.routes.prefixes,
        ),
    )


__all__ = ("register_configured_routes_for_site",)
