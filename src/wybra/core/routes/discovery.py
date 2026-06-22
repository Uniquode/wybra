"""Conventional configured-module route discovery.

Callers provide an explicit configured module list; this module never scans
installed packages. Missing optional route modules are empty contributions. Route
modules are imported only when requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wybra.core.composition import CompositionError
from wybra.core.conventions import (
    ROUTE_EXPORT_ATTRIBUTE,
    ROUTE_MODULE,
    module_surface_name,
)
from wybra.core.diagnostics import surface_message
from wybra.core.module_discovery import (
    find_module_spec,
    import_surface_module,
    require_configured_module,
)

if TYPE_CHECKING:
    from wybra.core.routes.registration import ModuleRouters


@dataclass(frozen=True, slots=True)
class ModuleSurface:
    module_name: str
    module_routers: ModuleRouters = field(default_factory=dict)


def discover_module_surfaces(
    module_names: tuple[str, ...],
    *,
    include_routes: bool = False,
) -> tuple[ModuleSurface, ...]:
    return tuple(
        discover_module_surface(
            module_name,
            include_routes=include_routes,
        )
        for module_name in module_names
    )


def discover_module_surface(
    module_name: str,
    *,
    include_routes: bool = False,
) -> ModuleSurface:
    require_configured_module(module_name)
    if include_routes:
        module_routers = discover_module_routers(module_name)
    else:
        module_routers = {}
    return ModuleSurface(
        module_name=module_name,
        module_routers=module_routers,
    )


def discover_module_routers(module_name: str) -> ModuleRouters:
    from fastapi.routing import APIRouter

    route_module_name = module_surface_name(module_name, ROUTE_MODULE)
    if not _has_route_module(route_module_name):
        return {}

    route_module = import_surface_module(
        route_module_name,
        surface="Configured module route module",
    )
    module_routers = getattr(route_module, ROUTE_EXPORT_ATTRIBUTE, None)
    if not isinstance(module_routers, Mapping):
        raise CompositionError(
            surface_message(
                "Route module",
                route_module_name,
                (
                    f"must expose `{ROUTE_EXPORT_ATTRIBUTE}` as a "
                    "mapping of router labels to fastapi.APIRouter instances."
                ),
            )
        )

    routers: dict[str, APIRouter] = {}
    for label, router in module_routers.items():
        if not isinstance(label, str) or not label.strip():
            raise CompositionError(
                surface_message(
                    "Route module",
                    route_module_name,
                    "must use non-blank string router labels.",
                )
            )
        if not isinstance(router, APIRouter):
            raise CompositionError(
                surface_message(
                    "Route module",
                    route_module_name,
                    f"router label {label!r} must be a fastapi.APIRouter instance.",
                )
            )

        routers[label] = router

    return routers


def _has_route_module(route_module_name: str) -> bool:
    spec = find_module_spec(route_module_name)
    if spec is None:
        return False
    return getattr(spec, "origin", None) is not None


__all__ = [
    "ModuleSurface",
    "ROUTE_EXPORT_ATTRIBUTE",
    "ROUTE_MODULE",
    "discover_module_routers",
    "discover_module_surface",
    "discover_module_surfaces",
]
