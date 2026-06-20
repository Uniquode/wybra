"""Static asset validation helpers."""

from __future__ import annotations

import os
from importlib import import_module
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wybra.assets.serving import static_sources_from_modules
from wybra.core.resources import PackageResourceSource, first_existing_resource
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check
from wybra.utils.paths import resolve_project_path

if TYPE_CHECKING:
    from wybra.core.composition import AppConfig

ResourceForValidation = Traversable | Path
STATIC_ASSET_CAPABILITY_MARKER = "provides_static_asset_capability"


class AssetValidationSettings(Protocol):
    project_root: Path
    static_root: Path | None
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...

    @property
    def uses_filesystem_static_root(self) -> bool: ...


def record_asset_collection_root_check(
    settings: AssetValidationSettings,
    checks: list[ValidationCheck],
    errors: list[str],
) -> None:
    root = asset_collection_root(settings)
    if root is None:
        record_check(
            checks,
            errors,
            passed=True,
            description="static asset collection root is not configured",
        )
        return

    problem = _collection_root_problem(root)
    record_check(
        checks,
        errors,
        passed=problem is None,
        description=f"static asset collection root is usable: {root}",
        error=problem,
    )


def validate_assets(settings: AssetValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    static_url_path = static_url_path_for_validation(settings)

    record_static_asset_provider_check(settings, checks, errors)
    if static_url_path is not None:
        record_check(
            checks,
            errors,
            passed=bool(static_url_path.strip()),
            description=f"static URL path is configured: {static_url_path}",
            error="Static URL path must not be empty.",
        )
    record_asset_collection_root_check(settings, checks, errors)

    try:
        static_sources = static_sources_for_validation(settings)
    except Exception as exc:  # pragma: no cover - defensive boundary
        record_check(
            checks,
            errors,
            passed=False,
            description="static asset sources load",
            error=f"Static asset source validation failed: {exc}",
        )
    else:
        source_names = ", ".join(
            f"{source.package}:{source.directory}" for source in static_sources
        )
        record_check(
            checks,
            errors,
            passed=True,
            description=(
                "static asset sources load"
                + (f": {source_names}" if source_names else ": none configured")
            ),
        )

    return ValidationResult(name="assets", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"assets": validate_assets}


def record_static_asset_provider_check(
    settings: AssetValidationSettings,
    checks: list[ValidationCheck],
    errors: list[str],
) -> None:
    modules = settings.modules
    if not _has_static_asset_provider(modules):
        record_check(
            checks,
            errors,
            passed=True,
            description="static asset capability provider is not required",
        )
        return

    record_check(
        checks,
        errors,
        passed=True,
        description="static asset capability provider is configured",
    )


def static_url_path_for_validation(settings: AssetValidationSettings) -> str | None:
    app_config = settings.app_config
    if app_config is None:
        return None
    return app_config.assets.url_path


def asset_collection_root(settings: AssetValidationSettings) -> Path | None:
    app_config = settings.app_config
    if app_config is None:
        return None
    return resolve_project_path(app_config.project_root, app_config.assets.root)


def static_sources_for_validation(
    settings: AssetValidationSettings,
) -> tuple[PackageResourceSource, ...]:
    if settings.uses_filesystem_static_root:
        return ()

    return static_sources_from_modules(settings.modules)


def static_resource_for_validation(
    settings: AssetValidationSettings,
    static_sources: tuple[PackageResourceSource, ...],
    asset: str,
) -> ResourceForValidation | None:
    if static_sources:
        return first_existing_resource(static_sources, asset)
    if not settings.uses_filesystem_static_root:
        return None

    if settings.static_root is None:
        return None

    asset_path = settings.static_root / asset
    return asset_path if asset_path.is_file() else None


def static_location_for_validation(
    settings: AssetValidationSettings,
    asset: str,
) -> str:
    if settings.uses_filesystem_static_root and settings.static_root is not None:
        return str(settings.static_root / asset)

    return asset


def _collection_root_problem(root: Path) -> str | None:
    if root.exists():
        if root.is_dir():
            return None
        return f"Static asset collection root is not a directory: {root}"

    parent = _nearest_existing_parent(root)
    if parent is None:
        return f"Static asset collection root parent does not exist: {root.parent}"
    if not parent.is_dir():
        return f"Static asset collection root parent is not a directory: {parent}"
    if not os.access(parent, os.W_OK | os.X_OK):
        return (
            "Static asset collection root cannot be created because parent is not "
            f"writable: {parent}"
        )
    return None


def _has_static_asset_provider(modules: tuple[str, ...]) -> bool:
    for module_name in modules:
        if module_name == "wybra.assets":
            return True
        if _declares_static_asset_capability_provider(module_name):
            return True
    return False


def _declares_static_asset_capability_provider(module_name: str) -> bool:
    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return False
    return getattr(module, STATIC_ASSET_CAPABILITY_MARKER, False) is True


def _nearest_existing_parent(path: Path) -> Path | None:
    parent = path.parent
    while parent != parent.parent:
        if parent.exists():
            return parent
        parent = parent.parent
    return parent if parent.exists() else None


__all__ = (
    "AssetValidationSettings",
    "ResourceForValidation",
    "STATIC_ASSET_CAPABILITY_MARKER",
    "asset_collection_root",
    "record_asset_collection_root_check",
    "record_static_asset_provider_check",
    "static_location_for_validation",
    "static_resource_for_validation",
    "static_sources_for_validation",
    "static_url_path_for_validation",
    "validate_assets",
    "validation_targets",
)
