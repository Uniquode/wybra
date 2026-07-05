"""Configured-module data surface discovery.

`wybra.db` imports configured root modules and their optional conventional
`<module>.models` surfaces only when callers ask for data metadata. Model
metadata is returned in configured order, while migration version locations are
discovered beside the owning module and later composed into one Alembic graph.
No host application settings, routes, or startup modules should be imported
here.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Final

from sqlalchemy import MetaData

from wybra.core.conventions import (
    MIGRATION_RESOURCE_DIRECTORY,
    MIGRATION_VERSIONS_DIRECTORY,
    MODEL_METADATA_ATTRIBUTE,
    MODEL_SURFACE_MODULE,
    module_surface_name,
)
from wybra.core.diagnostics import configured_module_message, surface_message
from wybra.core.modules import CORE_MODULES

_MAX_AVAILABLE_ATTRIBUTE_NAMES: Final = 20
_METADATA_CACHE_SIZE: Final = 32


class DataCompositionError(ValueError):
    """Raised when configured data module surfaces are invalid."""


def model_packages_from_modules(
    module_names: tuple[str, ...],
) -> tuple[str, ...]:
    model_packages: list[str] = []
    for module_name in _data_modules(module_names):
        _require_configured_module(module_name)
        model_package = model_package_name(module_name)
        if _find_module_spec(model_package) is not None:
            model_packages.append(model_package)

    return tuple(model_packages)


def model_package_name(module_name: str) -> str:
    return module_surface_name(module_name, MODEL_SURFACE_MODULE)


def discover_model_metadata(module_name: str) -> MetaData | None:
    model_package = model_package_name(module_name)
    if _find_module_spec(model_package) is None:
        return None

    return metadata_from_model_package(model_package)


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

    return (
        Path(package_file).resolve().parent
        / MIGRATION_RESOURCE_DIRECTORY
        / MIGRATION_VERSIONS_DIRECTORY
    )


def discover_migration_version_locations(module_name: str) -> tuple[Path, ...]:
    _require_configured_module(module_name)
    module = import_module(module_name)
    package_file = getattr(module, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        return ()

    version_location = (
        Path(package_file).resolve().parent
        / MIGRATION_RESOURCE_DIRECTORY
        / MIGRATION_VERSIONS_DIRECTORY
    )
    if version_location.is_dir():
        return (version_location,)

    return ()


@lru_cache(maxsize=_METADATA_CACHE_SIZE)
def metadata_from_model_package(package_name: str) -> MetaData:
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

    metadata = getattr(module, MODEL_METADATA_ATTRIBUTE, None)
    if not isinstance(metadata, MetaData):
        raise DataCompositionError(
            surface_message(
                "Model package",
                package_name,
                (
                    "must expose SQLAlchemy metadata as a top-level "
                    f"`{MODEL_METADATA_ATTRIBUTE}` attribute. Module origin: "
                    f"{_module_origin(module)}. Available attributes: "
                    f"{_available_attribute_summary(module)}."
                ),
            )
        )

    return metadata


def _require_configured_module(module_name: str) -> None:
    if _find_module_spec(module_name) is None:
        raise DataCompositionError(
            configured_module_message(module_name, "could not be imported.")
        )


def _data_modules(module_names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*CORE_MODULES, *module_names)))


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


def _available_attribute_summary(module: object) -> str:
    names = sorted(name for name in dir(module) if not name.startswith("__"))
    if not names:
        return "<none>"

    visible_names = names[:_MAX_AVAILABLE_ATTRIBUTE_NAMES]
    summary = ", ".join(visible_names)
    hidden_count = len(names) - len(visible_names)
    if hidden_count > 0:
        return f"{summary}, ... (+{hidden_count} more)"

    return summary


def _module_origin(module: object) -> str:
    origin = getattr(module, "__file__", None)
    return origin if isinstance(origin, str) and origin else "<unknown>"
