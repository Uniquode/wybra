"""Conventional configured-module surface discovery.

Callers provide an explicit configured module list; this module never scans
installed packages. Missing optional surfaces are empty contributions. Route
and context modules are imported only when requested, while template/static
resource discovery only inspects package resources. Template and static source
helpers return sources in configured order so earlier configured modules have
override precedence over later foundation modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module, resources
from importlib.util import find_spec
from types import ModuleType
from typing import TYPE_CHECKING

from wybra.core.composition import CompositionError
from wybra.core.conventions import (
    CONTEXT_SURFACE_MODULE,
    ROUTE_EXPORT_ATTRIBUTE,
    ROUTE_SURFACE_MODULE,
    TEMPLATE_RESOURCE_DIRECTORY,
    module_surface_name,
)
from wybra.core.diagnostics import configured_module_message, surface_message
from wybra.core.resources import PackageResourceSource
from wybra.web.context import ContextProvider, get_context_providers

if TYPE_CHECKING:
    from wybra.web.routes.registration import ModuleRouters


@dataclass(frozen=True, slots=True)
class ModuleSurface:
    module_name: str
    module_routers: ModuleRouters = field(default_factory=dict)
    template_sources: tuple[PackageResourceSource, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()


def discover_module_surfaces(
    module_names: tuple[str, ...],
    *,
    include_routes: bool = False,
    include_context: bool = False,
) -> tuple[ModuleSurface, ...]:
    return tuple(
        discover_module_surface(
            module_name,
            include_routes=include_routes,
            include_context=include_context,
        )
        for module_name in module_names
    )


def discover_module_surface(
    module_name: str,
    *,
    include_routes: bool = False,
    include_context: bool = False,
) -> ModuleSurface:
    _require_configured_module(module_name)
    if include_routes:
        module_routers = discover_module_routers(module_name)
    else:
        module_routers = {}
    return ModuleSurface(
        module_name=module_name,
        module_routers=module_routers,
        template_sources=discover_template_sources(module_name),
        context_providers=(
            discover_context_providers(module_name) if include_context else ()
        ),
    )


def discover_module_routers(module_name: str) -> ModuleRouters:
    from fastapi.routing import APIRouter

    route_module_name = module_surface_name(module_name, ROUTE_SURFACE_MODULE)
    if _find_module_spec(route_module_name) is None:
        return {}

    route_module = _import_surface_module(route_module_name)
    module_routers = getattr(route_module, ROUTE_EXPORT_ATTRIBUTE, None)
    if not isinstance(module_routers, Mapping):
        raise CompositionError(
            surface_message(
                "Route surface",
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
                    "Route surface",
                    route_module_name,
                    "must use non-blank string router labels.",
                )
            )
        if not isinstance(router, APIRouter):
            raise CompositionError(
                surface_message(
                    "Route surface",
                    route_module_name,
                    f"router label {label!r} must be a fastapi.APIRouter instance.",
                )
            )

        routers[label] = router

    return routers


def discover_template_sources(module_name: str) -> tuple[PackageResourceSource, ...]:
    return _discover_resource_sources(module_name, TEMPLATE_RESOURCE_DIRECTORY)


def template_sources_from_modules(
    module_names: tuple[str, ...],
) -> tuple[PackageResourceSource, ...]:
    return _resource_sources_from_modules(module_names, discover_template_sources)


def discover_context_providers(module_name: str) -> tuple[ContextProvider, ...]:
    context_module_name = module_surface_name(module_name, CONTEXT_SURFACE_MODULE)
    if _find_module_spec(context_module_name) is None:
        return ()

    _import_surface_module(context_module_name)
    return get_context_providers(context_module_name)


def context_providers_from_modules(
    module_names: tuple[str, ...],
) -> tuple[ContextProvider, ...]:
    providers: list[ContextProvider] = []
    for module_name in module_names:
        _require_configured_module(module_name)
        providers.extend(discover_context_providers(module_name))

    return tuple(providers)


def _discover_resource_sources(
    module_name: str,
    directory: str,
) -> tuple[PackageResourceSource, ...]:
    if _resource_directory_exists(module_name, directory):
        return (PackageResourceSource(package=module_name, directory=directory),)

    return ()


def _resource_sources_from_modules(
    module_names: tuple[str, ...],
    discover_sources: Callable[[str], tuple[PackageResourceSource, ...]],
) -> tuple[PackageResourceSource, ...]:
    sources: list[PackageResourceSource] = []
    for module_name in module_names:
        _require_configured_module(module_name)
        sources.extend(discover_sources(module_name))

    return tuple(sources)


def _resource_directory_exists(module_name: str, directory: str) -> bool:
    try:
        return resources.files(module_name).joinpath(directory).is_dir()
    except (ModuleNotFoundError, TypeError):
        return False


def _require_configured_module(module_name: str) -> None:
    if _find_module_spec(module_name) is None:
        raise CompositionError(
            configured_module_message(module_name, "could not be imported.")
        )


def _find_module_spec(module_name: str) -> object | None:
    try:
        return find_spec(module_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, module_name):
            return None

        raise


def _import_surface_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, module_name):
            raise CompositionError(
                surface_message(
                    "Configured module surface",
                    module_name,
                    "could not be imported.",
                )
            ) from None

        raise


def _missing_configured_package(exc: ModuleNotFoundError, package_name: str) -> bool:
    missing_name = exc.name
    return missing_name is not None and (
        missing_name == package_name or package_name.startswith(f"{missing_name}.")
    )


__all__ = [
    "CONTEXT_SURFACE_MODULE",
    "ModuleSurface",
    "ROUTE_EXPORT_ATTRIBUTE",
    "ROUTE_SURFACE_MODULE",
    "TEMPLATE_RESOURCE_DIRECTORY",
    "discover_context_providers",
    "context_providers_from_modules",
    "discover_module_routers",
    "discover_module_surface",
    "discover_module_surfaces",
    "discover_template_sources",
    "template_sources_from_modules",
]
