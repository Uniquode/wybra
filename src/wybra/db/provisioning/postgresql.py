from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from wybra.db.provisioning.core import (
    DatabaseFamily,
    DatabaseMaintenanceRequest,
    DatabaseMaintenanceTask,
    DatabaseProvisioningConfigurationError,
    DatabaseProvisioningOperationError,
    DestroyDatabaseRequest,
    ProvisioningContext,
    ProvisioningPhase,
    ProvisioningPhaseResult,
    ProvisioningStatus,
)
from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.sql import RenderedSql, ident, param, quote_sql_identifier, render_sql

_DEFAULT_POSTGRESQL_SCHEMA = "public"
_DEFAULT_SERVICE_ACCOUNT_DATABASE = "postgres"
_MIGRATION_RECORDER_TABLE = "tortoise_migrations"
_POSTGRESQL_MAINTENANCE_TASKS = (
    DatabaseMaintenanceTask(
        name="repair-privileges",
        description="Reapply runtime role grants and default privileges.",
        recommended_frequency="after migrations or role changes",
    ),
    DatabaseMaintenanceTask(
        name="migration-state",
        description="Report Tortoise migration recorder state.",
    ),
    DatabaseMaintenanceTask(
        name="analyse",
        description="Refresh PostgreSQL planner statistics.",
        recommended_frequency="after large data changes",
    ),
    DatabaseMaintenanceTask(
        name="validate-extensions",
        description="Validate PostgreSQL extension prerequisites.",
    ),
)


class PostgreSQLConnection(Protocol):
    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchval(self, query: str, *args: object) -> object: ...

    async def close(self) -> object: ...


PostgreSQLConnector = Callable[
    [Mapping[str, object]],
    Awaitable[PostgreSQLConnection],
]


class PostgreSQLProvisioner:
    family: DatabaseFamily = "postgresql"

    def __init__(self, connector: PostgreSQLConnector | None = None) -> None:
        self._connector = connector or _connect_asyncpg

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_postgresql_context(context)
        return await self._initialise(context)

    async def _initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="init")
        target_database = _target_database(runtime_connection)
        service_database = _service_account_database(service_connection)
        _ensure_lifecycle_database_is_distinct(target_database, service_database)
        service_user = _required_credential(
            service_connection,
            "user",
            "PostgreSQL init requires a service-account database user.",
        )
        app_user = _required_credential(
            runtime_connection,
            "user",
            "PostgreSQL init requires a runtime application database user.",
        )
        app_password = _optional_credential(runtime_connection, "password")
        schema = _schema_name(runtime_connection)

        maintenance = await self._connect(service_connection, database=service_database)
        try:
            results: list[ProvisioningPhaseResult] = []
            database_exists = await _database_exists(maintenance, target_database)
            if database_exists:
                await _ensure_database_owner(maintenance, target_database, service_user)
                results.append(
                    _result(
                        "init",
                        "skipped",
                        f"PostgreSQL database already exists: {target_database}",
                    )
                )
            else:
                await _execute(
                    maintenance,
                    render_sql(
                        t"CREATE DATABASE {ident(target_database)} "
                        t"OWNER {ident(service_user)}",
                        dialect="postgresql",
                    ),
                )
                results.append(
                    _result(
                        "init",
                        "created",
                        f"Created PostgreSQL database: {target_database}",
                    )
                )

            role_created = await _ensure_application_role(
                maintenance,
                app_user=app_user,
                app_password=app_password,
            )
            results.append(
                _result(
                    "init",
                    "created" if role_created else "skipped",
                    (
                        f"Created PostgreSQL application role: {app_user}"
                        if role_created
                        else f"PostgreSQL application role already exists: {app_user}"
                    ),
                )
            )
        finally:
            await maintenance.close()

        target = await self._connect(service_connection, database=target_database)
        try:
            schema_created = await _ensure_schema(target, schema, service_user)
            results.append(
                _result(
                    "init",
                    "created" if schema_created else "skipped",
                    (
                        f"Created PostgreSQL schema: {schema}"
                        if schema_created
                        else f"PostgreSQL schema already exists: {schema}"
                    ),
                )
            )
            await _repair_privileges(
                target,
                database=target_database,
                schema=schema,
                service_user=service_user,
                app_user=app_user,
            )
            results.append(
                _result(
                    "init",
                    "skipped",
                    f"Repaired PostgreSQL runtime privileges for role: {app_user}",
                )
            )
            results.append(await _migration_state_result(target, schema))
            return tuple(results)
        finally:
            await target.close()

    async def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_postgresql_context(context)
        return await self._destroy(context, request)

    async def _destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="destroy")
        target_database = _target_database(runtime_connection)
        service_database = _service_account_database(service_connection)
        service_user = _required_credential(
            service_connection,
            "user",
            "PostgreSQL destroy requires a service-account database user.",
        )
        app_user = _required_credential(
            runtime_connection,
            "user",
            "PostgreSQL destroy requires a runtime application database user.",
        )
        _ensure_destroy_confirmed(target_database, service_database, request)

        maintenance = await self._connect(service_connection, database=service_database)
        try:
            results: list[ProvisioningPhaseResult] = []
            database_exists = await _database_exists(maintenance, target_database)
            role_exists = await _role_exists(maintenance, app_user)
            role_has_external_dependencies = (
                role_exists
                and app_user != service_user
                and await _role_has_external_dependencies(
                    maintenance,
                    role=app_user,
                    target_database=target_database,
                )
            )
            if database_exists:
                await _terminate_database_sessions(maintenance, target_database)
                await _execute(
                    maintenance,
                    render_sql(
                        t"DROP DATABASE {ident(target_database)}",
                        dialect="postgresql",
                    ),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed PostgreSQL database: {target_database}",
                    )
                )
            else:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"PostgreSQL database already absent: {target_database}",
                    )
                )

            if not role_exists:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"PostgreSQL application role already absent: {app_user}",
                    )
                )
            elif app_user == service_user:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped PostgreSQL application role removal because "
                        "runtime and service-account roles are the same.",
                    )
                )
            elif role_has_external_dependencies:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped PostgreSQL application role removal because "
                        f"the role has dependencies outside {target_database}.",
                    )
                )
            else:
                await _execute(
                    maintenance,
                    render_sql(t"DROP ROLE {ident(app_user)}", dialect="postgresql"),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed PostgreSQL application role: {app_user}",
                    )
                )
            return tuple(results)
        finally:
            await maintenance.close()

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_postgresql_context(context)
        return _POSTGRESQL_MAINTENANCE_TASKS

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_postgresql_context(context)
        return await self._run_maintenance(context, request)

    async def _run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        task = request.task.strip()
        if task not in {
            maintenance_task.name for maintenance_task in _POSTGRESQL_MAINTENANCE_TASKS
        }:
            raise DatabaseProvisioningConfigurationError(
                f"Unknown postgresql maintenance task: {request.task}."
            )

        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(
            context,
            phase=f"maintenance:{task}",
        )
        target_database = _target_database(runtime_connection)
        service_user = _required_credential(
            service_connection,
            "user",
            "PostgreSQL maintenance requires a service-account database user.",
        )
        app_user = _required_credential(
            runtime_connection,
            "user",
            "PostgreSQL maintenance requires a runtime application database user.",
        )
        schema = _schema_name(runtime_connection)

        target = await self._connect(service_connection, database=target_database)
        try:
            if task == "repair-privileges":
                await _repair_privileges(
                    target,
                    database=target_database,
                    schema=schema,
                    service_user=service_user,
                    app_user=app_user,
                )
                return (
                    _result(
                        "maintenance",
                        "skipped",
                        f"Repaired PostgreSQL runtime privileges for role: {app_user}",
                    ),
                )
            if task == "migration-state":
                return (
                    await _migration_state_result(
                        target,
                        schema,
                        phase="maintenance",
                    ),
                )
            if task == "analyse":
                await _execute(target, render_sql(t"ANALYSE", dialect="postgresql"))
                return (
                    _result(
                        "maintenance",
                        "skipped",
                        "Ran PostgreSQL ANALYSE.",
                    ),
                )
            return (
                _result(
                    "maintenance",
                    "noop",
                    "No PostgreSQL extensions are required by default.",
                ),
            )
        finally:
            await target.close()

    def quote_identifier(self, identifier: str) -> str:
        return quote_sql_identifier(identifier)

    async def _connect(
        self,
        connection: ResolvedDatabaseConnection,
        *,
        database: str,
    ) -> PostgreSQLConnection:
        credentials = dict(connection.credentials)
        credentials["database"] = database
        try:
            return await self._connector(credentials)
        except DatabaseProvisioningConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseProvisioningOperationError(
                f"Failed to connect to PostgreSQL database: {database}"
            ) from exc


async def _connect_asyncpg(credentials: Mapping[str, object]) -> PostgreSQLConnection:
    try:
        asyncpg = importlib.import_module("asyncpg")
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise DatabaseProvisioningConfigurationError(
            "PostgreSQL provisioning requires the asyncpg database extra."
        ) from exc

    connect = cast(Callable[..., Awaitable[PostgreSQLConnection]], asyncpg.connect)
    return await connect(**dict(credentials))


async def _database_exists(connection: PostgreSQLConnection, database: str) -> bool:
    value = await _fetchval(
        connection,
        render_sql(
            t"SELECT EXISTS ("
            t"SELECT 1 FROM pg_database WHERE datname = {param(database)}"
            t")",
            dialect="postgresql",
        ),
    )
    return bool(value)


async def _role_exists(connection: PostgreSQLConnection, role: str) -> bool:
    value = await _fetchval(
        connection,
        render_sql(
            t"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {param(role)})",
            dialect="postgresql",
        ),
    )
    return bool(value)


async def _schema_exists(connection: PostgreSQLConnection, schema: str) -> bool:
    value = await _fetchval(
        connection,
        render_sql(
            t"SELECT EXISTS ("
            t"SELECT 1 FROM information_schema.schemata "
            t"WHERE schema_name = {param(schema)}"
            t")",
            dialect="postgresql",
        ),
    )
    return bool(value)


async def _ensure_database_owner(
    connection: PostgreSQLConnection,
    database: str,
    owner: str,
) -> None:
    current_owner = await _fetchval(
        connection,
        render_sql(
            t"SELECT pg_catalog.pg_get_userbyid(datdba) "
            t"FROM pg_database WHERE datname = {param(database)}",
            dialect="postgresql",
        ),
    )
    if current_owner == owner:
        return
    await _execute(
        connection,
        render_sql(
            t"ALTER DATABASE {ident(database)} OWNER TO {ident(owner)}",
            dialect="postgresql",
        ),
    )


async def _ensure_application_role(
    connection: PostgreSQLConnection,
    *,
    app_user: str,
    app_password: str | None,
) -> bool:
    role_exists = await _role_exists(connection, app_user)
    if role_exists:
        if app_password is not None:
            await _execute_raw(
                connection,
                (
                    f"ALTER ROLE {quote_sql_identifier(app_user)} "
                    f"WITH PASSWORD {_postgresql_string_literal(app_password)}"
                ),
            )
        return False

    password_clause = (
        f" PASSWORD {_postgresql_string_literal(app_password)}"
        if app_password is not None
        else ""
    )
    await _execute_raw(
        connection,
        f"CREATE ROLE {quote_sql_identifier(app_user)} LOGIN{password_clause}",
    )
    return True


async def _terminate_database_sessions(
    connection: PostgreSQLConnection,
    database: str,
) -> None:
    await _execute(
        connection,
        render_sql(
            t"SELECT pg_terminate_backend(pid) "
            t"FROM pg_stat_activity "
            t"WHERE datname = {param(database)} "
            t"AND pid <> pg_backend_pid()",
            dialect="postgresql",
        ),
    )


async def _role_has_external_dependencies(
    connection: PostgreSQLConnection,
    *,
    role: str,
    target_database: str,
) -> bool:
    value = await _fetchval(
        connection,
        render_sql(
            t"SELECT EXISTS ("
            t"SELECT 1 FROM pg_shdepend dep "
            t"JOIN pg_roles role ON role.oid = dep.refobjid "
            t"WHERE role.rolname = {param(role)} "
            t"AND dep.deptype IN ('o', 'a') "
            t"AND dep.dbid <> COALESCE("
            t"(SELECT oid FROM pg_database WHERE datname = {param(target_database)}), "
            t"0"
            t")"
            t")",
            dialect="postgresql",
        ),
    )
    return bool(value)


async def _ensure_schema(
    connection: PostgreSQLConnection,
    schema: str,
    owner: str,
) -> bool:
    schema_exists = await _schema_exists(connection, schema)
    if not schema_exists:
        await _execute(
            connection,
            render_sql(
                t"CREATE SCHEMA {ident(schema)} AUTHORIZATION {ident(owner)}",
                dialect="postgresql",
            ),
        )
        return True

    current_owner = await _fetchval(
        connection,
        render_sql(
            t"SELECT schema_owner FROM information_schema.schemata "
            t"WHERE schema_name = {param(schema)}",
            dialect="postgresql",
        ),
    )
    if current_owner != owner:
        await _execute(
            connection,
            render_sql(
                t"ALTER SCHEMA {ident(schema)} OWNER TO {ident(owner)}",
                dialect="postgresql",
            ),
        )
    return False


async def _repair_privileges(
    connection: PostgreSQLConnection,
    *,
    database: str,
    schema: str,
    service_user: str,
    app_user: str,
) -> None:
    statements = (
        render_sql(
            t"GRANT CONNECT ON DATABASE {ident(database)} TO {ident(app_user)}",
            dialect="postgresql",
        ),
        render_sql(
            t"GRANT USAGE ON SCHEMA {ident(schema)} TO {ident(app_user)}",
            dialect="postgresql",
        ),
        render_sql(
            t"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            t"IN SCHEMA {ident(schema)} TO {ident(app_user)}",
            dialect="postgresql",
        ),
        render_sql(
            t"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES "
            t"IN SCHEMA {ident(schema)} TO {ident(app_user)}",
            dialect="postgresql",
        ),
        render_sql(
            t"ALTER DEFAULT PRIVILEGES FOR ROLE {ident(service_user)} "
            t"IN SCHEMA {ident(schema)} "
            t"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ident(app_user)}",
            dialect="postgresql",
        ),
        render_sql(
            t"ALTER DEFAULT PRIVILEGES FOR ROLE {ident(service_user)} "
            t"IN SCHEMA {ident(schema)} "
            t"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {ident(app_user)}",
            dialect="postgresql",
        ),
    )
    for statement in statements:
        await _execute(connection, statement)


async def _migration_state_result(
    connection: PostgreSQLConnection,
    schema: str,
    *,
    phase: ProvisioningPhase = "init",
) -> ProvisioningPhaseResult:
    table_exists = await _fetchval(
        connection,
        render_sql(
            t"SELECT EXISTS ("
            t"SELECT 1 FROM information_schema.tables "
            t"WHERE table_schema = {param(schema)} "
            t"AND table_name = {param(_MIGRATION_RECORDER_TABLE)}"
            t")",
            dialect="postgresql",
        ),
    )
    if not table_exists:
        return _result(
            phase,
            "noop",
            "Tortoise migration recorder table is absent.",
        )

    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM {ident(schema)}.{ident(_MIGRATION_RECORDER_TABLE)}",
            dialect="postgresql",
        ),
    )
    migration_count = _normalise_count(count)
    if migration_count == 0:
        return _result(
            phase,
            "noop",
            "Tortoise migration recorder table is empty.",
        )
    return _result(
        phase,
        "noop",
        f"Tortoise migration recorder contains {migration_count} record(s).",
    )


async def _execute(connection: PostgreSQLConnection, statement: RenderedSql) -> object:
    try:
        return await connection.execute(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "PostgreSQL provisioning statement failed."
        ) from exc


async def _execute_raw(connection: PostgreSQLConnection, statement: str) -> object:
    try:
        return await connection.execute(statement)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "PostgreSQL provisioning statement failed."
        ) from exc


async def _fetchval(connection: PostgreSQLConnection, statement: RenderedSql) -> object:
    try:
        return await connection.fetchval(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "PostgreSQL provisioning query failed."
        ) from exc


def _ensure_postgresql_context(context: ProvisioningContext) -> None:
    if context.family != "postgresql":
        raise DatabaseProvisioningConfigurationError(
            f"Provisioner postgresql cannot handle database family {context.family}."
        )


def _service_account_connection(
    context: ProvisioningContext,
    *,
    phase: str,
) -> ResolvedDatabaseConnection:
    connection = context.provisioning_connection
    if connection is None:
        raise DatabaseProvisioningConfigurationError(
            f"Database {phase} requires service-account credentials."
        )
    _required_credential(
        connection,
        "user",
        f"Database {phase} requires a service-account database user.",
    )
    _required_credential(
        connection,
        "password",
        f"Database {phase} requires a service-account database password.",
    )
    return connection


def _target_database(connection: ResolvedDatabaseConnection) -> str:
    return _required_credential(
        connection,
        "database",
        "PostgreSQL lifecycle requires a target database.",
    )


def _service_account_database(connection: ResolvedDatabaseConnection) -> str:
    if connection.sa_database is not None:
        return connection.sa_database
    return _DEFAULT_SERVICE_ACCOUNT_DATABASE


def _schema_name(connection: ResolvedDatabaseConnection) -> str:
    value = connection.credentials.get("schema")
    if value is None:
        return _DEFAULT_POSTGRESQL_SCHEMA
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DatabaseProvisioningConfigurationError(
        "PostgreSQL schema must be a non-blank string."
    )


def _required_credential(
    connection: ResolvedDatabaseConnection,
    name: str,
    message: str,
) -> str:
    value = connection.credentials.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DatabaseProvisioningConfigurationError(message)


def _optional_credential(
    connection: ResolvedDatabaseConnection,
    name: str,
) -> str | None:
    value = connection.credentials.get(name)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DatabaseProvisioningConfigurationError(
        f"PostgreSQL credential {name} must be a non-blank string."
    )


def _ensure_destroy_confirmed(
    target_database: str,
    service_database: str,
    request: DestroyDatabaseRequest,
) -> None:
    if request.confirm.strip() != target_database:
        raise DatabaseProvisioningConfigurationError(
            "PostgreSQL destroy confirmation does not match the configured database."
        )
    _ensure_lifecycle_database_is_distinct(target_database, service_database)


def _ensure_lifecycle_database_is_distinct(
    target_database: str,
    service_database: str,
) -> None:
    if service_database == target_database:
        raise DatabaseProvisioningConfigurationError(
            "PostgreSQL lifecycle must connect through a different "
            "service-account database."
        )


def _postgresql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalise_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise DatabaseProvisioningOperationError(
            "PostgreSQL migration recorder count was invalid."
        )
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise DatabaseProvisioningOperationError(
            "PostgreSQL migration recorder count was invalid."
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseProvisioningOperationError(
            "PostgreSQL migration recorder count was invalid."
        ) from exc


def _result(
    phase: ProvisioningPhase,
    status: ProvisioningStatus,
    message: str,
) -> ProvisioningPhaseResult:
    return ProvisioningPhaseResult(
        family="postgresql",
        phase=phase,
        status=status,
        message=message,
    )


__all__ = ("PostgreSQLProvisioner",)
