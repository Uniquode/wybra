"""Configured-module data surface discovery.

`wybra.db` imports configured root modules and their optional conventional
`<module>.models` surfaces only when callers ask for data model modules. Model
module names are returned in configured order, while migration version locations
are discovered beside the owning module for Tortoise migration commands.
No host application settings, routes, or startup modules should be imported
here.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Final

from tortoise.models import Model

from wybra.core.conventions import (
    MIGRATION_RESOURCE_DIRECTORY,
    MODEL_SURFACE_MODULE,
    module_surface_name,
)
from wybra.core.diagnostics import configured_module_message, surface_message
from wybra.core.modules import CORE_MODULES

_MODEL_PACKAGE_CACHE_SIZE: Final = 32
DATA_MODULE_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    "wybra.profile": ("wybra.auth", "wybra.media"),
}


class DataCompositionError(ValueError):
    """Raised when configured data module surfaces are invalid."""


def model_packages_from_modules(
    module_names: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        model_package
        for model_packages in model_packages_by_module(module_names).values()
        for model_package in model_packages
    )


def model_packages_by_module(
    module_names: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    packages: dict[str, tuple[str, ...]] = {}
    for module_name in _data_modules(module_names):
        _require_configured_module(module_name)
        model_package = discover_model_package(module_name)
        if model_package is not None:
            packages[module_name] = (model_package,)

    return packages


def discover_model_package(module_name: str) -> str | None:
    model_package = model_package_name(module_name)
    if _find_module_spec(model_package) is None:
        return None

    if _model_package_has_model(model_package):
        return model_package
    return None


def model_package_name(module_name: str) -> str:
    return module_surface_name(module_name, MODEL_SURFACE_MODULE)


def migration_version_locations_from_modules(
    module_names: tuple[str, ...],
) -> tuple[Path, ...]:
    version_locations: list[Path] = []
    for module_name in _data_modules(module_names):
        _require_configured_module(module_name)
        version_locations.extend(discover_migration_version_locations(module_name))

    return tuple(version_locations)


def migration_version_location_for_configured_module(
    module_name: str,
    configured_modules: tuple[str, ...],
) -> Path:
    if module_name not in configured_modules:
        raise DataCompositionError(
            configured_module_message(
                module_name,
                "is not present in the active module configuration.",
            )
        )

    return migration_version_location_for_module(module_name)


def migration_version_location_for_module(module_name: str) -> Path:
    _require_configured_module(module_name)
    module = import_module(module_name)
    package_file = getattr(module, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        raise DataCompositionError(
            configured_module_message(
                module_name,
                "does not have a filesystem package location.",
            )
        )

    return Path(package_file).resolve().parent / MIGRATION_RESOURCE_DIRECTORY


def discover_migration_version_locations(module_name: str) -> tuple[Path, ...]:
    _require_configured_module(module_name)
    module = import_module(module_name)
    package_file = getattr(module, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        return ()

    version_location = (
        Path(package_file).resolve().parent / MIGRATION_RESOURCE_DIRECTORY
    )
    if version_location.is_dir():
        return (version_location,)

    return ()


@lru_cache(maxsize=_MODEL_PACKAGE_CACHE_SIZE)
def _model_package_has_model(package_name: str) -> bool:
    try:
        module = import_module(package_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, package_name):
            raise DataCompositionError(
                surface_message(
                    "Model package",
                    package_name,
                    "could not be imported.",
                )
            ) from None

        raise
    return _has_tortoise_model(module)


def _require_configured_module(module_name: str) -> None:
    if _find_module_spec(module_name) is None:
        raise DataCompositionError(
            configured_module_message(module_name, "could not be imported.")
        )


def _data_modules(module_names: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = [*CORE_MODULES]
    for module_name in module_names:
        expanded.extend(DATA_MODULE_DEPENDENCIES.get(module_name, ()))
        expanded.append(module_name)
    return tuple(dict.fromkeys(expanded))


def _find_module_spec(module_name: str) -> object | None:
    try:
        return find_spec(module_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, module_name):
            return None

        raise


def _missing_configured_package(exc: ModuleNotFoundError, package_name: str) -> bool:
    missing_name = exc.name
    return missing_name is not None and (
        missing_name == package_name or package_name.startswith(f"{missing_name}.")
    )


def _has_tortoise_model(module: object) -> bool:
    return any(
        isinstance(value, type) and issubclass(value, Model) and value is not Model
        for value in vars(module).values()
    )
