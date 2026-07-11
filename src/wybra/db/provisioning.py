from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.urls import DatabaseBackend

DatabaseFamily = Literal["sqlite", "postgresql", "mysql", "mssql", "oracle"]
ProvisioningStatus = Literal["created", "skipped", "noop"]
ProvisioningPhase = Literal["init", "destroy", "maintenance"]


class DatabaseProvisioningError(RuntimeError):
    """Base class for database lifecycle provisioning failures."""


class DatabaseProvisioningConfigurationError(DatabaseProvisioningError):
    """Raised when database lifecycle configuration is invalid."""


class DatabaseProvisioningOperationError(DatabaseProvisioningError):
    """Raised when a database lifecycle operation fails."""


@dataclass(frozen=True, slots=True)
class ProvisioningPhaseResult:
    family: DatabaseFamily
    phase: str
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
        return (
            ProvisioningPhaseResult(
                family=self.family,
                phase="init",
                status="skipped",
                message="SQLite provisioning is handled by migration initialisation.",
            ),
        )

    def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        del request
        _ensure_family(context, self.family)
        return (
            ProvisioningPhaseResult(
                family=self.family,
                phase="destroy",
                status="noop",
                message="SQLite database file removal is handled by filesystem tools.",
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

    environment = Environment(  # nosec B701 - internal SQL templates are not HTML.
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
