from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Any

from wybra.core.conventions import (
    VALIDATION_SURFACE_MODULE,
    VALIDATION_TARGETS_ATTRIBUTE,
    module_surface_name,
)
from wybra.core.diagnostics import (
    configured_module_message,
    surface_message,
    validation_target_message,
)
from wybra.tools.validation.core import ValidationResult

ValidationTarget = Callable[[Any], ValidationResult]


@dataclass(frozen=True, slots=True)
class DiscoveredValidationTargets:
    targets: dict[str, ValidationTarget]
    origins: dict[str, str]


class ValidationDiscoveryError(ValueError):
    """Raised when configured module validation surfaces are invalid."""


def discover_validation_targets(
    module_names: Sequence[str],
) -> dict[str, ValidationTarget]:
    return discover_validation_target_details(module_names).targets


def discover_validation_target_details(
    module_names: Sequence[str],
) -> DiscoveredValidationTargets:
    targets: dict[str, ValidationTarget] = {}
    target_origins: dict[str, str] = {}

    for module_name in module_names:
        _require_configured_module(module_name)
        surface_name = validation_surface_name(module_name)
        if _find_module_spec(surface_name) is None:
            continue

        surface = _import_validation_surface(surface_name)
        for target_name, target in _targets_from_surface(surface_name, surface).items():
            if target_name in targets:
                previous_surface = target_origins[target_name]
                raise ValidationDiscoveryError(
                    validation_target_message(
                        target_name,
                        surface_name,
                        f"duplicates target from {previous_surface!r}.",
                    )
                )
            targets[target_name] = target
            target_origins[target_name] = surface_name

    return DiscoveredValidationTargets(targets=targets, origins=target_origins)


def validation_target_names(module_names: Sequence[str]) -> tuple[str, ...]:
    return tuple(discover_validation_targets(module_names))


def validation_surface_name(module_name: str) -> str:
    return module_surface_name(module_name, VALIDATION_SURFACE_MODULE)


def _targets_from_surface(
    surface_name: str,
    surface: ModuleType,
) -> dict[str, ValidationTarget]:
    validation_targets = getattr(surface, VALIDATION_TARGETS_ATTRIBUTE, None)
    if not isinstance(validation_targets, Mapping):
        raise ValidationDiscoveryError(
            surface_message(
                "Validation surface",
                surface_name,
                (
                    f"must expose `{VALIDATION_TARGETS_ATTRIBUTE}` as a "
                    "mapping of target names to callables."
                ),
            )
        )

    targets: dict[str, ValidationTarget] = {}
    for target_name, target in validation_targets.items():
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValidationDiscoveryError(
                surface_message(
                    "Validation surface",
                    surface_name,
                    (
                        "contains an invalid target name; target names must be "
                        "non-empty strings."
                    ),
                )
            )
        if not callable(target):
            raise ValidationDiscoveryError(
                validation_target_message(
                    target_name,
                    surface_name,
                    "must be callable.",
                )
            )
        targets[target_name] = target

    return targets


def _require_configured_module(module_name: str) -> None:
    if _find_module_spec(module_name) is None:
        raise ValidationDiscoveryError(
            configured_module_message(module_name, "could not be imported.")
        )


def _find_module_spec(module_name: str) -> object | None:
    try:
        return find_spec(module_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, module_name):
            return None

        raise


def _import_validation_surface(surface_name: str) -> ModuleType:
    try:
        return import_module(surface_name)
    except ModuleNotFoundError as exc:
        if _missing_configured_package(exc, surface_name):
            raise ValidationDiscoveryError(
                surface_message(
                    "Validation surface",
                    surface_name,
                    "could not be imported.",
                )
            ) from None

        raise


def _missing_configured_package(exc: ModuleNotFoundError, package_name: str) -> bool:
    missing_name = exc.name
    return missing_name is not None and (
        missing_name == package_name or package_name.startswith(f"{missing_name}.")
    )
