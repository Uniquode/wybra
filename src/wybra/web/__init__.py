"""Reusable FastAPI, Starlette, route, and form infrastructure."""

from __future__ import annotations

from wybra.assets import StaticAssetCapability
from wybra.config.transforms import to_url_path
from wybra.site import Site, SiteCapabilityProxy
from wybra.web.config import module_config
from wybra.web.errors import ErrorHandlerOptions, register_error_handlers


async def setup_site(site: Site) -> None:
    asset_capability = site.capability_proxy(StaticAssetCapability)

    register_error_handlers(
        site.app,
        options=ErrorHandlerOptions(
            static_mount_path=lambda: _optional_static_mount_path(asset_capability)
        ),
    )


def _optional_static_mount_path(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> str | None:
    capability = proxy.optional()
    if capability is None:
        return None
    return to_url_path(capability.url_path, name="StaticAssetCapability.url_path")


__all__ = [
    "module_config",
    "setup_site",
]
