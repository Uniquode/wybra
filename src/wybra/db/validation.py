"""Database and migration validation helpers owned by ``wybra.db``."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from wybra.db.migrate import migration_script_root
from wybra.db.persistence import (
    is_memory_database_url,
    is_supported_database_url,
)
from wybra.db.surfaces import (
    DataCompositionError,
    migration_version_locations_from_modules,
    model_packages_from_modules,
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
            description="database URL uses supported async SQLAlchemy driver",
            error=(
                "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://."
            ),
        )

    _record_sqlite_persistence_check(settings, checks, errors)
    has_migrations_root = _record_migration_root_checks(settings, checks, errors)

    record_check(
        checks,
        errors,
        passed=has_migrations_root,
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


def _record_migration_root_checks(
    settings: PersistenceValidationSettings,
    checks: list[ValidationCheck],
    errors: list[str],
) -> bool:
    migrations_root = migration_script_root(settings.migrations_root)
    has_migrations_root = record_check(
        checks,
        errors,
        passed=migrations_root.is_dir(),
        description=f"Alembic migrations root exists: {migrations_root}",
        error=f"Missing Alembic migrations root: {migrations_root}",
    )
    if not has_migrations_root:
        return False

    migration_root_valid = True
    for required_file in ("env.py", "script.py.mako"):
        required_path = migrations_root.joinpath(required_file)
        migration_root_valid = (
            record_check(
                checks,
                errors,
                passed=required_path.is_file(),
                description=f"Alembic migration file exists: {required_file}",
                error=f"Missing Alembic migration file: {required_path}",
            )
            and migration_root_valid
        )

    try:
        model_packages = model_packages_from_modules(settings.modules)
        version_locations = migration_version_locations_from_modules(settings.modules)
    except DataCompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="module migration version locations load",
            error=f"Module migration version location discovery failed: {exc}",
        )
        return False

    migration_root_valid = (
        record_check(
            checks,
            errors,
            passed=not model_packages or bool(version_locations),
            description=(
                "module migration version locations exist: "
                + ", ".join(str(path) for path in version_locations)
            ),
            error=(
                "At least one configured module migration version location is required."
            ),
        )
        and migration_root_valid
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
        migration_root_valid = (
            record_check(
                checks,
                errors,
                passed=bool(revision_files),
                description="Alembic migration revision exists",
                error="At least one Alembic migration revision is required.",
            )
            and migration_root_valid
        )
    else:
        record_check(
            checks,
            errors,
            passed=True,
            description="Alembic migration revisions optional without model modules",
            error=(
                "Alembic migration revisions are only required when configured "
                "modules expose model metadata."
            ),
        )

    return migration_root_valid


validation_targets = {"persistence": validate_persistence}


__all__ = (
    "PersistenceValidationSettings",
    "validate_persistence",
    "validation_targets",
)
