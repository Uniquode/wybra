"""Template source and context discovery for configured modules."""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources

from wybra.core.conventions import (
    CONTEXT_SURFACE_MODULE,
    TEMPLATE_RESOURCE_DIRECTORY,
    module_surface_name,
)
from wybra.core.module_discovery import (
    find_module_spec,
    import_surface_module,
    require_configured_module,
)
from wybra.core.resources import PackageResourceSource
from wybra.template.context import ContextProvider, get_context_providers


def discover_template_sources(module_name: str) -> tuple[PackageResourceSource, ...]:
    return _discover_resource_sources(module_name, TEMPLATE_RESOURCE_DIRECTORY)


def template_sources_from_modules(
    module_names: tuple[str, ...],
) -> tuple[PackageResourceSource, ...]:
    return _resource_sources_from_modules(module_names, discover_template_sources)


def discover_context_providers(module_name: str) -> tuple[ContextProvider, ...]:
    context_module_name = module_surface_name(module_name, CONTEXT_SURFACE_MODULE)
    if find_module_spec(context_module_name) is None:
        return ()

    import_surface_module(
        context_module_name,
        surface="Configured template context surface",
    )
    return get_context_providers(context_module_name)


def context_providers_from_modules(
    module_names: tuple[str, ...],
) -> tuple[ContextProvider, ...]:
    providers: list[ContextProvider] = []
    for module_name in module_names:
        require_configured_module(module_name)
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
        require_configured_module(module_name)
        sources.extend(discover_sources(module_name))
    return tuple(sources)


def _resource_directory_exists(module_name: str, directory: str) -> bool:
    try:
        return resources.files(module_name).joinpath(directory).is_dir()
    except (ModuleNotFoundError, TypeError):
        return False


__all__ = (
    "CONTEXT_SURFACE_MODULE",
    "TEMPLATE_RESOURCE_DIRECTORY",
    "context_providers_from_modules",
    "discover_context_providers",
    "discover_template_sources",
    "template_sources_from_modules",
)
