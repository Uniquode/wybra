from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.urls import DatabaseBackend, database_backend_for_url

if TYPE_CHECKING:
    from wybra.db.provisioning.aws import AwsRdsMetadataClient

DatabaseFamily = Literal["sqlite", "postgresql", "mysql", "mariadb", "mssql", "oracle"]
MaintenanceCredentialScope = Literal["runtime", "service_account"]
ProvisioningStatus = Literal[
    "created",
    "removed",
    "skipped",
    "updated",
    "noop",
    "unsupported",
]
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
    phase: ProvisioningPhase
    status: ProvisioningStatus
    message: str


@dataclass(frozen=True, slots=True)
class DestroyDatabaseRequest:
    confirm: str


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceRequest:
    task: str
    confirm: str | None = None


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceTask:
    name: str
    description: str
    credential_scope: MaintenanceCredentialScope = "service_account"
    requires_confirmation: bool = False
    recommended_frequency: str | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        description = self.description.strip()
        if not name:
            raise DatabaseProvisioningConfigurationError(
                "Maintenance task name must not be blank."
            )
        if not description:
            raise DatabaseProvisioningConfigurationError(
                "Maintenance task description must not be blank."
            )
        if self.credential_scope not in {"runtime", "service_account"}:
            raise DatabaseProvisioningConfigurationError(
                "Maintenance task credential scope is invalid."
            )
        recommended_frequency = (
            self.recommended_frequency.strip()
            if self.recommended_frequency is not None
            else None
        )
        if recommended_frequency == "":
            raise DatabaseProvisioningConfigurationError(
                "Maintenance task recommended frequency must not be blank."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "recommended_frequency", recommended_frequency)


REPAIR_PRIVILEGES_TASK = DatabaseMaintenanceTask(
    name="repair-privs",
    description="Reapply runtime database privileges.",
    recommended_frequency="after migrations or credential changes",
)
TORTOISE_MIGRATIONS_TASK = DatabaseMaintenanceTask(
    name="migrations",
    description="Report Tortoise migration recorder state.",
)


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


class DatabaseProvisioner(Protocol):
    family: DatabaseFamily

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    async def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]: ...

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]: ...

    async def clear_test_data(
        self,
        connection: BaseDBAsyncClient,
        table_names: tuple[str, ...],
    ) -> None: ...

    def quote_identifier(self, identifier: str) -> str: ...


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
    if backend.scheme == "mariadb":
        return "mariadb"
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
    provisioners: Mapping[DatabaseFamily, DatabaseProvisioner] | None = None,
) -> DatabaseProvisioner:
    provisioner = (DEFAULT_PROVISIONERS if provisioners is None else provisioners).get(
        family
    )
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
    aws_metadata_client: AwsRdsMetadataClient | None = None,
) -> ProvisioningContext:
    family = database_family_for_backend(runtime_connection.backend)
    context = ProvisioningContext(
        family=family,
        runtime_connection=runtime_connection,
        provisioning_connection=(
            runtime_connection if family == "sqlite" else provisioning_connection
        ),
        project_root=project_root.resolve(),
        modules=modules,
    )
    if runtime_connection.aws is None and (
        context.provisioning_connection is None
        or context.provisioning_connection.aws is None
    ):
        return context

    from wybra.db.provisioning.aws import validate_aws_managed_database_context

    return validate_aws_managed_database_context(
        context,
        metadata_client=aws_metadata_client,
    )


async def initialise_database(
    context: ProvisioningContext,
) -> tuple[ProvisioningPhaseResult, ...]:
    return await provisioner_for_family(context.family).initialise(context)


async def destroy_database(
    context: ProvisioningContext,
    request: DestroyDatabaseRequest,
) -> tuple[ProvisioningPhaseResult, ...]:
    if not request.confirm.strip():
        raise DatabaseProvisioningConfigurationError(
            "Destroy confirmation must not be blank."
        )
    return await provisioner_for_family(context.family).destroy(context, request)


async def run_database_maintenance(
    context: ProvisioningContext,
    request: DatabaseMaintenanceRequest,
) -> tuple[ProvisioningPhaseResult, ...]:
    task_name = request.task.strip()
    if not task_name:
        raise DatabaseProvisioningConfigurationError(
            "Maintenance task name must not be blank."
        )
    task = next(
        (
            available_task
            for available_task in database_maintenance_tasks(context)
            if available_task.name == task_name
        ),
        None,
    )
    if task is not None and task.requires_confirmation and request.confirm != task.name:
        raise DatabaseProvisioningConfigurationError(
            "Maintenance task requires confirmation."
        )
    normalised_request = (
        request
        if request.task == task_name
        else DatabaseMaintenanceRequest(task=task_name, confirm=request.confirm)
    )
    return await provisioner_for_family(context.family).run_maintenance(
        context,
        normalised_request,
    )


def database_maintenance_tasks(
    context: ProvisioningContext,
) -> tuple[DatabaseMaintenanceTask, ...]:
    return provisioner_for_family(context.family).maintenance_tasks(context)


async def clear_test_database_data(
    connection: BaseDBAsyncClient,
    *,
    database_url: str,
    table_names: tuple[str, ...],
) -> None:
    """Clear migrated application tables using the active database family."""
    if not table_names:
        return
    backend = database_backend_for_url(database_url)
    if backend is None:
        raise DatabaseProvisioningConfigurationError(
            "Test database URL uses an unsupported database scheme."
        )
    family = database_family_for_backend(backend)
    await provisioner_for_family(family).clear_test_data(connection, table_names)


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


def _default_provisioners() -> Mapping[DatabaseFamily, DatabaseProvisioner]:
    from wybra.db.provisioning.mariadb import MariaDBProvisioner
    from wybra.db.provisioning.mssql import SQLServerProvisioner
    from wybra.db.provisioning.mysql import MySQLProvisioner
    from wybra.db.provisioning.postgresql import PostgreSQLProvisioner
    from wybra.db.provisioning.sqlite import SQLiteProvisioner
    from wybra.db.provisioning.unsupported import UnsupportedFamilyProvisioner

    return {
        "sqlite": SQLiteProvisioner(),
        "postgresql": PostgreSQLProvisioner(),
        "mysql": MySQLProvisioner(),
        "mariadb": MariaDBProvisioner(),
        "mssql": SQLServerProvisioner(),
        "oracle": UnsupportedFamilyProvisioner("oracle"),
    }


DEFAULT_PROVISIONERS: Mapping[DatabaseFamily, DatabaseProvisioner] = (
    _default_provisioners()
)


__all__ = (
    "CredentialTransition",
    "DatabaseFamily",
    "DatabaseMaintenanceRequest",
    "DatabaseMaintenanceTask",
    "DatabaseProvisioner",
    "DatabaseProvisioningConfigurationError",
    "DatabaseProvisioningError",
    "DatabaseProvisioningOperationError",
    "DestroyDatabaseRequest",
    "MaintenanceCredentialScope",
    "ProvisioningContext",
    "ProvisioningPhase",
    "ProvisioningPhaseResult",
    "REPAIR_PRIVILEGES_TASK",
    "TORTOISE_MIGRATIONS_TASK",
    "database_family_for_backend",
    "database_maintenance_tasks",
    "clear_test_database_data",
    "destroy_database",
    "initialise_database",
    "provisioner_for_family",
    "provisioning_context",
    "run_database_maintenance",
)
