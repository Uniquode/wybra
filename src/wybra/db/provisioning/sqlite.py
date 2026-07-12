from __future__ import annotations

from pathlib import Path

from wybra.db.provisioning.core import (
    DatabaseFamily,
    DatabaseMaintenanceRequest,
    DatabaseMaintenanceTask,
    DatabaseProvisioningConfigurationError,
    DatabaseProvisioningOperationError,
    DestroyDatabaseRequest,
    ProvisioningContext,
    ProvisioningPhaseResult,
    _ensure_family,
)
from wybra.db.sql import quote_sql_identifier
from wybra.db.urls import is_memory_database_url, parse_sqlite_database_url

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class SQLiteProvisioner:
    family: DatabaseFamily = "sqlite"

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        target = _sqlite_file_target(context)
        if target is None:
            return (
                ProvisioningPhaseResult(
                    family=self.family,
                    phase="init",
                    status="noop",
                    message="SQLite in-memory database has no persistent file target.",
                ),
            )
        if target.exists():
            _ensure_sqlite_file_target(target)
            return _sqlite_initialise_skipped_result(self.family, target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=False)
        except FileExistsError:
            _ensure_sqlite_file_target(target)
            return _sqlite_initialise_skipped_result(self.family, target)
        except OSError as exc:
            raise DatabaseProvisioningOperationError(
                f"Failed to initialise SQLite database file: {target}"
            ) from exc
        return (
            ProvisioningPhaseResult(
                family=self.family,
                phase="init",
                status="created",
                message=f"Initialised SQLite database file: {target}",
            ),
        )

    async def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        target = _sqlite_file_target(context)
        if target is None:
            return (
                ProvisioningPhaseResult(
                    family=self.family,
                    phase="destroy",
                    status="noop",
                    message="SQLite in-memory database has no persistent file target.",
                ),
            )
        _ensure_sqlite_file_target(target)
        _ensure_sqlite_destroy_confirmed(target, request)
        removed_paths = _remove_sqlite_file_targets(target)
        if not removed_paths:
            return (
                ProvisioningPhaseResult(
                    family=self.family,
                    phase="destroy",
                    status="skipped",
                    message=f"SQLite database file already absent: {target}",
                ),
            )

        return (
            ProvisioningPhaseResult(
                family=self.family,
                phase="destroy",
                status="removed",
                message=(
                    "Removed SQLite database file target: "
                    f"{target} ({len(removed_paths)} file(s))"
                ),
            ),
        )

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_family(context, self.family)
        return ()

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        raise DatabaseProvisioningConfigurationError(
            f"Unknown sqlite maintenance task: {request.task}."
        )

    def quote_identifier(self, identifier: str) -> str:
        return quote_sql_identifier(identifier)


def _sqlite_initialise_skipped_result(
    family: DatabaseFamily,
    target: Path,
) -> tuple[ProvisioningPhaseResult, ...]:
    return (
        ProvisioningPhaseResult(
            family=family,
            phase="init",
            status="skipped",
            message=f"SQLite database file already exists: {target}",
        ),
    )


def _sqlite_file_target(context: ProvisioningContext) -> Path | None:
    connection = context.runtime_connection
    file_path = connection.credentials.get("file_path")
    if file_path is not None:
        return _normalise_sqlite_file_path(file_path, project_root=context.project_root)

    database_url = connection.database_url
    if database_url is None:
        raise DatabaseProvisioningConfigurationError(
            "SQLite database configuration must identify a file path or :memory:."
        )
    if is_memory_database_url(database_url):
        return None

    sqlite_url = parse_sqlite_database_url(database_url)
    if sqlite_url is None:
        raise DatabaseProvisioningConfigurationError(
            "SQLite database URL must identify a file path or :memory:."
        )
    return _normalise_sqlite_file_path(
        sqlite_url.path,
        project_root=context.project_root,
        path_is_absolute=sqlite_url.is_absolute,
    )


def _normalise_sqlite_file_path(
    value: object,
    *,
    project_root: Path,
    path_is_absolute: bool | None = None,
) -> Path | None:
    if not isinstance(value, str | Path):
        raise DatabaseProvisioningConfigurationError(
            "SQLite database file path must be a string or path."
        )
    if isinstance(value, str):
        if not value.strip():
            raise DatabaseProvisioningConfigurationError(
                "SQLite database file path must not be blank."
            )
        if value.strip() == ":memory:":
            return None
        path = Path(value.strip())
    else:
        path = value

    if path_is_absolute and not path.is_absolute():
        raise DatabaseProvisioningConfigurationError(
            "SQLite database file path is not usable on this host."
        )
    if not path.is_absolute():
        path = project_root / path
    target = path.resolve()
    if not target.name:
        raise DatabaseProvisioningConfigurationError(
            "SQLite database file path must identify a file."
        )
    return target


def _ensure_sqlite_file_target(target: Path) -> None:
    if target.is_dir():
        raise DatabaseProvisioningConfigurationError(
            f"SQLite database target is a directory: {target}"
        )


def _ensure_sqlite_destroy_confirmed(
    target: Path,
    request: DestroyDatabaseRequest,
) -> None:
    confirm = request.confirm.strip()
    accepted = {target.name, target.as_posix(), str(target)}
    if confirm not in accepted:
        raise DatabaseProvisioningConfigurationError(
            "SQLite destroy confirmation does not match the configured target."
        )


def _remove_sqlite_file_targets(target: Path) -> tuple[Path, ...]:
    removed: list[Path] = []
    for candidate in (target, *_sqlite_sidecar_targets(target)):
        if not candidate.exists():
            continue
        if candidate.is_dir():
            raise DatabaseProvisioningConfigurationError(
                f"SQLite destroy target is a directory: {candidate}"
            )
        try:
            candidate.unlink()
        except OSError as exc:
            raise DatabaseProvisioningOperationError(
                f"Failed to remove SQLite database file target: {candidate}"
            ) from exc
        removed.append(candidate)
    return tuple(removed)


def _sqlite_sidecar_targets(target: Path) -> tuple[Path, ...]:
    return tuple(
        target.with_name(f"{target.name}{suffix}")
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    )


__all__ = ("SQLiteProvisioner",)
