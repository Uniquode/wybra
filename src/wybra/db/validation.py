"""Database and migration validation helpers owned by ``wybra.db``."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from wybra.db.persistence import (
    is_memory_database_url,
    is_supported_database_url,
)
from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.surfaces import (
    DataCompositionError,
    discover_migration_version_locations,
    discover_model_package,
    model_package_name,
)
from wybra.db.urls import (
    database_url_support_error,
    is_database_backend_available,
    parse_sqlite_database_url,
    redact_database_url,
)
from wybra.tools.validation.core import (
    ValidationCheck,
    ValidationResult,
    record_check,
)


class PersistenceValidationSettings(Protocol):
    database_url: str | None
    database_connection: ResolvedDatabaseConnection | None
    migrations_root: Path | None

    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_persistence(settings: PersistenceValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    database_connection = getattr(settings, "database_connection", None)
    database_url = settings.database_url

    has_database_connection = record_check(
        checks,
        errors,
        passed=database_connection is not None
        or bool(database_url is not None and database_url.strip()),
        description=_database_connection_description(database_connection, database_url),
        error=_database_connection_error(database_url),
    )
    if database_connection is not None:
        record_check(
            checks,
            errors,
            passed=is_database_backend_available(database_connection.backend),
            description="database backend is available",
            error=database_url_support_error(
                f"{database_connection.backend.scheme}://"
            ),
        )
    elif has_database_connection and database_url is not None:
        record_check(
            checks,
            errors,
            passed=is_supported_database_url(database_url),
            description="database URL uses an available Tortoise database scheme",
            error=database_url_support_error(database_url),
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
    database_connection = getattr(settings, "database_connection", None)
    if database_connection is not None:
        if database_connection.backend.tortoise_scheme != "sqlite":
            return
        file_path = database_connection.credentials.get("file_path")
        record_check(
            checks,
            errors,
            passed=file_path != ":memory:",
            description="SQLite database uses persistent file storage",
            error="SQLite database must not force in-memory storage.",
        )
        return

    if settings.database_url is None:
        return
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


def _database_connection_description(
    database_connection: ResolvedDatabaseConnection | None,
    database_url: str | None,
) -> str:
    if database_connection is not None:
        return (
            "database connection is configured: "
            + database_connection.redacted_description
        )
    if database_url is None:
        return "database connection is configured"
    return f"database URL is configured: {redact_database_url(database_url)}"


def _database_connection_error(database_url: str | None) -> str:
    if database_url is not None:
        return "Database URL must not be empty."
    return "Database connection must be configured."


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
