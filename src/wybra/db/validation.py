"""Database and migration validation helpers owned by ``wybra.db``."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from wybra.db.persistence import (
    is_memory_database_url,
    is_supported_database_url,
)
from wybra.db.surfaces import (
    DataCompositionError,
    discover_migration_version_locations,
    discover_model_package,
    model_package_name,
)
from wybra.db.urls import parse_sqlite_database_url, redact_database_url
from wybra.tools.validation.core import (
    ValidationCheck,
    ValidationResult,
    record_check,
)


class PersistenceValidationSettings(Protocol):
    database_url: str
    migrations_root: Path | None

    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_persistence(settings: PersistenceValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    display_database_url = redact_database_url(settings.database_url)

    has_database_url = record_check(
        checks,
        errors,
        passed=bool(settings.database_url.strip()),
        description=f"database URL is configured: {display_database_url}",
        error="Database URL must not be empty.",
    )
    if has_database_url:
        record_check(
            checks,
            errors,
            passed=is_supported_database_url(settings.database_url),
            description="database URL uses supported async database driver",
            error=(
                "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://."
            ),
        )

    _record_sqlite_persistence_check(settings, checks, errors)
    has_migration_resources = _record_migration_resource_checks(
        settings,
        checks,
        errors,
    )

    record_check(
        checks,
        errors,
        passed=has_migration_resources,
        description=(
            "development database initialisation command is available: "
            "uv run wybra-migrate init"
        ),
        error="Development database initialisation requires migrations.",
    )

    return ValidationResult(
        name="persistence",
        errors=tuple(errors),
        checks=tuple(checks),
    )


def _record_sqlite_persistence_check(
    settings: PersistenceValidationSettings,
    checks: list[ValidationCheck],
    errors: list[str],
) -> None:
    if is_memory_database_url(settings.database_url):
        record_check(
            checks,
            errors,
            passed=False,
            description="SQLite database URL uses persistent file storage",
            error="SQLite database URL must not force in-memory storage.",
        )
        return

    sqlite_url = parse_sqlite_database_url(settings.database_url)
    if sqlite_url is None:
        return

    record_check(
        checks,
        errors,
        passed=not is_memory_database_url(settings.database_url),
        description="SQLite database URL uses persistent file storage",
        error="SQLite database URL must not force in-memory storage.",
    )


def _record_migration_resource_checks(
    settings: PersistenceValidationSettings,
    checks: list[ValidationCheck],
    errors: list[str],
) -> bool:
    try:
        model_packages, version_locations = _configured_data_surfaces(settings.modules)
    except DataCompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="module migration version locations load",
            error=f"Module migration version location discovery failed: {exc}",
        )
        return False

    migration_resources_valid = record_check(
        checks,
        errors,
        passed=not model_packages or bool(version_locations),
        description=(
            "module Tortoise migration version locations exist: "
            + ", ".join(str(path) for path in version_locations)
        ),
        error=(
            "At least one configured module migration version location is required."
        ),
    )

    if model_packages:
        revision_files = tuple(
            sorted(
                path
                for version_location in version_locations
                for path in version_location.glob("*.py")
                if path.name != "__init__.py"
            )
        )
        migration_resources_valid = (
            record_check(
                checks,
                errors,
                passed=bool(revision_files),
                description="Tortoise migration file exists",
                error="At least one Tortoise migration file is required.",
            )
            and migration_resources_valid
        )
    else:
        record_check(
            checks,
            errors,
            passed=True,
            description="migration revisions optional without model modules",
            error=(
                "Migration revisions are only required when configured modules "
                "expose Tortoise models."
            ),
        )

    return migration_resources_valid


def _configured_data_surfaces(
    module_names: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    model_packages: list[str] = []
    version_locations: list[Path] = []
    for module_name in module_names:
        if discover_model_package(module_name) is not None:
            model_packages.append(model_package_name(module_name))
        version_locations.extend(discover_migration_version_locations(module_name))

    return tuple(model_packages), tuple(version_locations)


validation_targets = {"persistence": validate_persistence}


__all__ = (
    "PersistenceValidationSettings",
    "validate_persistence",
    "validation_targets",
)
