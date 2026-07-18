from __future__ import annotations

from importlib import import_module
from typing import Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import Response

from wybra.assets import StaticAssetCapability
from wybra.config.transforms import to_url_path
from wybra.errors.handlers import (
    DefaultErrorHandlingCapability,
    ErrorHandlerOptions,
    register_error_handlers,
)
from wybra.errors.mappings import ErrorMapping
from wybra.site import Site, SiteCapabilityProxy


@runtime_checkable
class ErrorHandlingCapability(Protocol):
    async def response_for_exception(
        self, request: Request, exc: Exception
    ) -> Response: ...


async def setup_site(site: Site) -> None:
    asset_capability = site.capability_proxy(StaticAssetCapability)
    register_error_handlers(
        site.app,
        options=ErrorHandlerOptions(
            static_mount_path=lambda: _optional_static_mount_path(asset_capability),
            mappings=discover_error_mappings(site.modules),
        ),
    )
    if not site.has_capability(ErrorHandlingCapability):
        site.provide_capability(
            ErrorHandlingCapability,
            DefaultErrorHandlingCapability(),
        )


def _optional_static_mount_path(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> str | None:
    capability = proxy.optional()
    if capability is None:
        return None
    return to_url_path(capability.url_path, name="StaticAssetCapability.url_path")


def discover_error_mappings(module_names: tuple[str, ...]) -> tuple[ErrorMapping, ...]:
    mappings: list[ErrorMapping] = []
    for module_name in module_names:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        raw_mappings = getattr(module, "error_mappings", ())
        if isinstance(raw_mappings, tuple | list):
            mappings.extend(
                mapping for mapping in raw_mappings if isinstance(mapping, ErrorMapping)
            )
    return tuple(mappings)
