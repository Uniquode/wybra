"""Shared configured-module discovery helpers."""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from types import ModuleType

from wybra.core.composition import CompositionError
from wybra.core.diagnostics import configured_module_message, surface_message


def require_configured_module(module_name: str) -> None:
    if find_module_spec(module_name) is None:
        raise CompositionError(
            configured_module_message(module_name, "could not be imported.")
        )


def find_module_spec(module_name: str) -> object | None:
    try:
        return find_spec(module_name)
    except ModuleNotFoundError as exc:
        if missing_configured_package(exc, module_name):
            return None
        raise


def import_surface_module(
    module_name: str,
    *,
    surface: str,
) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        if missing_configured_package(exc, module_name):
            raise CompositionError(
                surface_message(
                    surface,
                    module_name,
                    "could not be imported.",
                )
            ) from None

        raise


def missing_configured_package(
    exc: ModuleNotFoundError,
    package_name: str,
) -> bool:
    missing_name = exc.name
    return missing_name is not None and (
        missing_name == package_name or package_name.startswith(f"{missing_name}.")
    )


__all__ = (
    "find_module_spec",
    "import_surface_module",
    "missing_configured_package",
    "require_configured_module",
)
