from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.urls import (
    DatabaseBackend,
    is_memory_database_url,
    parse_sqlite_database_url,
)

DatabaseFamily = Literal["sqlite", "postgresql", "mysql", "mssql", "oracle"]
ProvisioningStatus = Literal["created", "removed", "skipped", "noop"]
ProvisioningPhase = Literal["init", "destroy", "maintenance"]
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class DatabaseProvisioningError(RuntimeError):
    """Base class for database lifecycle provisioning failures."""


class DatabaseProvisioningConfigurationError(DatabaseProvisioningError):
    """Raised when database lifecycle configuration is invalid."""


class DatabaseProvisioningOperationError(DatabaseProvisioningError):
    """Raised when a database lifecycle operation fails."""


@dataclass(frozen=True, slots=True)
class ProvisioningPhaseResult:
    family: DatabaseFamily
    phase: ProvisioningPhase
    status: ProvisioningStatus
    message: str


@dataclass(frozen=True, slots=True)
class DestroyDatabaseRequest:
    confirm: str


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceRequest:
    task: str


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceTask:
    name: str
    description: str
    recommended_frequency: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialTransition:
    """Current and previous values for explicit backend credential rotation."""

    current: str
    previous: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.current.strip():
            raise DatabaseProvisioningConfigurationError(
                "Current credential value must not be blank."
            )
        previous = tuple(value.strip() for value in self.previous if value.strip())
        if len(previous) != len(self.previous):
            raise DatabaseProvisioningConfigurationError(
                "Previous credential values must not be blank."
            )
        object.__setattr__(self, "current", self.current.strip())
        object.__setattr__(self, "previous", previous)


@dataclass(frozen=True, slots=True)
class ProvisioningContext:
    family: DatabaseFamily
    runtime_connection: ResolvedDatabaseConnection
    provisioning_connection: ResolvedDatabaseConnection | None
    project_root: Path
    modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProvisioningScriptContext:
    family: DatabaseFamily
    phase: ProvisioningPhase
    context: ProvisioningContext
    variables: Mapping[str, object]
    quote_identifier: Callable[[str], str]


class ProvisioningScript(Protocol):
    phase: ProvisioningPhase

    def run(
        self,
        context: ProvisioningScriptContext,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...


class DatabaseProvisioner(Protocol):
    family: DatabaseFamily

    def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]: ...

    def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    def quote_identifier(self, identifier: str) -> str: ...


class SQLiteProvisioner:
    family: DatabaseFamily = "sqlite"

    def initialise(
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

    def destroy(
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

    def run_maintenance(
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


class UnsupportedFamilyProvisioner:
    def __init__(self, family: DatabaseFamily) -> None:
        self.family = family

    def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        _require_service_account_connection(context, phase="init")
        raise DatabaseProvisioningOperationError(
            f"Database family {self.family} init provisioning is not implemented."
        )

    def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        del request
        _ensure_family(context, self.family)
        _require_service_account_connection(context, phase="destroy")
        raise DatabaseProvisioningOperationError(
            f"Database family {self.family} destroy is not implemented."
        )

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_family(context, self.family)
        return ()

    def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        _require_service_account_connection(
            context,
            phase=f"maintenance:{request.task}",
        )
        raise DatabaseProvisioningConfigurationError(
            f"Unknown {self.family} maintenance task: {request.task}."
        )

    def quote_identifier(self, identifier: str) -> str:
        return quote_sql_identifier(identifier)


DEFAULT_PROVISIONERS: Mapping[DatabaseFamily, DatabaseProvisioner] = {
    "sqlite": SQLiteProvisioner(),
    "postgresql": UnsupportedFamilyProvisioner("postgresql"),
    "mysql": UnsupportedFamilyProvisioner("mysql"),
    "mssql": UnsupportedFamilyProvisioner("mssql"),
    "oracle": UnsupportedFamilyProvisioner("oracle"),
}


def database_family_for_backend(backend: DatabaseBackend) -> DatabaseFamily:
    if backend.tortoise_scheme == "sqlite":
        return "sqlite"
    if backend.tortoise_scheme in {"asyncpg", "psycopg"} or backend.scheme in {
        "postgresql",
        "postgres",
        "asyncpg",
        "psycopg",
    }:
        return "postgresql"
    if backend.tortoise_scheme == "mysql":
        return "mysql"
    if backend.tortoise_scheme == "mssql":
        return "mssql"
    if backend.tortoise_scheme == "oracle":
        return "oracle"
    raise DatabaseProvisioningConfigurationError(
        f"Unsupported database family for backend: {backend.scheme}."
    )


def provisioner_for_family(
    family: DatabaseFamily,
    provisioners: Mapping[DatabaseFamily, DatabaseProvisioner] = DEFAULT_PROVISIONERS,
) -> DatabaseProvisioner:
    provisioner = provisioners.get(family)
    if provisioner is None:
        raise DatabaseProvisioningConfigurationError(
            f"Unsupported database family: {family}."
        )
    return provisioner


def provisioning_context(
    *,
    runtime_connection: ResolvedDatabaseConnection,
    provisioning_connection: ResolvedDatabaseConnection | None,
    project_root: Path,
    modules: tuple[str, ...],
) -> ProvisioningContext:
    family = database_family_for_backend(runtime_connection.backend)
    return ProvisioningContext(
        family=family,
        runtime_connection=runtime_connection,
        provisioning_connection=(
            runtime_connection if family == "sqlite" else provisioning_connection
        ),
        project_root=project_root.resolve(),
        modules=modules,
    )


def initialise_database(
    context: ProvisioningContext,
) -> tuple[ProvisioningPhaseResult, ...]:
    return provisioner_for_family(context.family).initialise(context)


def destroy_database(
    context: ProvisioningContext,
    request: DestroyDatabaseRequest,
) -> tuple[ProvisioningPhaseResult, ...]:
    if not request.confirm.strip():
        raise DatabaseProvisioningConfigurationError(
            "Destroy confirmation must not be blank."
        )
    return provisioner_for_family(context.family).destroy(context, request)


def run_database_maintenance(
    context: ProvisioningContext,
    request: DatabaseMaintenanceRequest,
) -> tuple[ProvisioningPhaseResult, ...]:
    if not request.task.strip():
        raise DatabaseProvisioningConfigurationError(
            "Maintenance task name must not be blank."
        )
    return provisioner_for_family(context.family).run_maintenance(context, request)


def quote_sql_identifier(identifier: str) -> str:
    """Quote an SQL identifier for provisioners that use standard quotes."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise DatabaseProvisioningConfigurationError(
            "SQL identifier must not be blank."
        )
    return '"' + identifier.strip().replace('"', '""') + '"'


def render_sql_template(
    template: str,
    *,
    variables: Mapping[str, object],
    quote_identifier: Callable[[str], str] = quote_sql_identifier,
) -> str:
    """Render an internal provisioning SQL template.

    Templates are Wybra-owned internal assets. The helper intentionally receives
    pre-validated values and does not log rendered SQL.
    """

    from jinja2 import Environment, StrictUndefined

    # These are SQL templates, NOT HTML templates. HTML autoescaping would not
    # provide SQL safety here and would produce invalid SQL for quoted values.
    environment = Environment(  # nosec B701 - SQL templates intentionally disable HTML escaping.
        autoescape=False,
        undefined=StrictUndefined,
    )
    environment.filters["quote_identifier"] = quote_identifier
    return environment.from_string(template).render(**variables)


def _ensure_family(context: ProvisioningContext, family: DatabaseFamily) -> None:
    if context.family != family:
        raise DatabaseProvisioningConfigurationError(
            f"Provisioner {family} cannot handle database family {context.family}."
        )


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


def _require_service_account_connection(
    context: ProvisioningContext,
    *,
    phase: str,
) -> ResolvedDatabaseConnection:
    connection = context.provisioning_connection
    if connection is None:
        raise DatabaseProvisioningConfigurationError(
            f"Database {phase} requires service-account credentials."
        )
    if connection.credentials.get("user") is None:
        raise DatabaseProvisioningConfigurationError(
            f"Database {phase} requires a service-account database user."
        )
    if connection.credentials.get("password") is None:
        raise DatabaseProvisioningConfigurationError(
            f"Database {phase} requires a service-account database password."
        )
    return connection


__all__ = (
    "DatabaseFamily",
    "DatabaseMaintenanceRequest",
    "DatabaseMaintenanceTask",
    "DatabaseProvisioner",
    "DatabaseProvisioningConfigurationError",
    "DatabaseProvisioningError",
    "DatabaseProvisioningOperationError",
    "DestroyDatabaseRequest",
    "ProvisioningContext",
    "ProvisioningPhase",
    "ProvisioningPhaseResult",
    "ProvisioningScript",
    "ProvisioningScriptContext",
    "SQLiteProvisioner",
    "UnsupportedFamilyProvisioner",
    "CredentialTransition",
    "database_family_for_backend",
    "destroy_database",
    "initialise_database",
    "provisioner_for_family",
    "provisioning_context",
    "quote_sql_identifier",
    "render_sql_template",
    "run_database_maintenance",
)
