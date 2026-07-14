from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from wybra.db.provisioning.core import (
    REPAIR_PRIVILEGES_TASK,
    TORTOISE_MIGRATIONS_TASK,
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
from wybra.db.sql import RenderedSql, ident, param, render_sql

_DEFAULT_SERVICE_ACCOUNT_DATABASE = "master"
_DEFAULT_SCHEMA = "dbo"
_DEFAULT_ROLE = "wybra_app"
_MIGRATION_RECORDER_TABLE = "tortoise_migrations"
_BASELINE_SERVER_PERMISSION = "CONNECT SQL"
_ALLOWED_ODBC_ATTRIBUTE_KEYS = {
    "applicationintent": "ApplicationIntent",
    "authentication": "Authentication",
    "connectiontimeout": "ConnectionTimeout",
    "encrypt": "Encrypt",
    "logintimeout": "LoginTimeout",
    "marsconnection": "MARSConnection",
    "multisubnetfailover": "MultiSubnetFailover",
    "timeout": "Timeout",
    "transparentnetworkipresolution": "TransparentNetworkIPResolution",
    "trustservercertificate": "TrustServerCertificate",
}
_MSSQL_MAINTENANCE_TASKS = (
    DatabaseMaintenanceTask(
        name=REPAIR_PRIVILEGES_TASK.name,
        description="Reapply runtime user database role grants.",
        recommended_frequency="after migrations or principal changes",
    ),
    TORTOISE_MIGRATIONS_TASK,
    DatabaseMaintenanceTask(
        name="prerequisites",
        description="Report SQL Server external setup prerequisites.",
    ),
)


class SQLServerConnection(Protocol):
    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchval(self, query: str, *args: object) -> object: ...

    async def close(self) -> object: ...


class SQLServerCursor(Protocol):
    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchone(self) -> object: ...

    def close(self) -> object: ...


SQLServerConnector = Callable[
    [Mapping[str, object]],
    Awaitable[SQLServerConnection],
]


class SQLServerProvisioner:
    family: DatabaseFamily = "mssql"

    def __init__(self, connector: SQLServerConnector | None = None) -> None:
        self._connector = connector or _connect_asyncodbc

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_mssql_context(context)
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="init")
        target_database = _target_database(runtime_connection)
        service_database = _service_account_database(service_connection)
        _ensure_lifecycle_database_is_distinct(target_database, service_database)
        app_user = _required_credential(
            runtime_connection,
            "user",
            "SQL Server init requires a runtime application database user.",
        )
        app_password = _optional_credential(runtime_connection, "password")
        schema = _schema_name(runtime_connection)
        role = _role_name(runtime_connection)

        maintenance = await self._connect(service_connection, database=service_database)
        try:
            results: list[ProvisioningPhaseResult] = []
            external_login_ready = False
            if app_password is None:
                if not await _login_exists(maintenance, app_user):
                    raise DatabaseProvisioningConfigurationError(
                        "SQL Server application login must exist when runtime "
                        "password is not configured."
                    )
                external_login_ready = True
            database_created = await _ensure_database(maintenance, target_database)
            results.append(
                _result(
                    "init",
                    "created" if database_created else "skipped",
                    (
                        f"Created SQL Server database: {target_database}"
                        if database_created
                        else f"SQL Server database already exists: {target_database}"
                    ),
                )
            )
            if external_login_ready:
                results.append(_external_login_ready_result(app_user))
            else:
                assert app_password is not None
                login_created = await _ensure_login(
                    maintenance,
                    app_user=app_user,
                    app_password=app_password,
                )
                results.append(
                    _result(
                        "init",
                        "created" if login_created else "skipped",
                        (
                            f"Created SQL Server application login: {app_user}"
                            if login_created
                            else (
                                "SQL Server application login already exists: "
                                f"{app_user}"
                            )
                        ),
                    )
                )
        finally:
            await maintenance.close()

        target = await self._connect(service_connection, database=target_database)
        try:
            schema_created = await _ensure_schema(target, schema)
            results.append(
                _result(
                    "init",
                    "created" if schema_created else "skipped",
                    (
                        f"Created SQL Server schema: {schema}"
                        if schema_created
                        else f"SQL Server schema already exists: {schema}"
                    ),
                )
            )
            user_created = await _ensure_database_user(
                target,
                app_user=app_user,
                schema=schema,
            )
            results.append(
                _result(
                    "init",
                    "created" if user_created else "skipped",
                    (
                        f"Created SQL Server database user: {app_user}"
                        if user_created
                        else f"SQL Server database user already exists: {app_user}"
                    ),
                )
            )
            role_created = await _ensure_role(target, role)
            results.append(
                _result(
                    "init",
                    "created" if role_created else "skipped",
                    (
                        f"Created SQL Server application role: {role}"
                        if role_created
                        else f"SQL Server application role already exists: {role}"
                    ),
                )
            )
            await _repair_privileges(
                target,
                schema=schema,
                role=role,
                app_user=app_user,
            )
            results.append(
                _result(
                    "init",
                    "updated",
                    f"Repaired SQL Server runtime privileges for role: {role}",
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
        _ensure_mssql_context(context)
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="destroy")
        target_database = _target_database(runtime_connection)
        service_database = _service_account_database(service_connection)
        service_user = _required_credential(
            service_connection,
            "user",
            "SQL Server destroy requires a service-account database user.",
        )
        app_user = _required_credential(
            runtime_connection,
            "user",
            "SQL Server destroy requires a runtime application database user.",
        )
        _ensure_destroy_confirmed(target_database, service_database, request)

        maintenance = await self._connect(service_connection, database=service_database)
        try:
            results: list[ProvisioningPhaseResult] = []
            database_exists = await _database_exists(maintenance, target_database)
            login_exists = await _login_exists(maintenance, app_user)
            login_has_external_dependencies = (
                login_exists
                and app_user != service_user
                and await _login_has_external_dependencies(
                    maintenance,
                    app_user=app_user,
                )
            )
            if database_exists:
                await _terminate_database_sessions(maintenance, target_database)
                await _execute(
                    maintenance,
                    render_sql(
                        t"DROP DATABASE {ident(target_database)}",
                        dialect="mssql",
                        quote_identifier=quote_mssql_identifier,
                    ),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed SQL Server database: {target_database}",
                    )
                )
            else:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"SQL Server database already absent: {target_database}",
                    )
                )

            if not login_exists:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"SQL Server application login already absent: {app_user}",
                    )
                )
            elif app_user == service_user:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped SQL Server application login removal because "
                        "runtime and service-account users are the same.",
                    )
                )
            elif login_has_external_dependencies:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped SQL Server application login removal because "
                        "server-level dependencies were detected.",
                    )
                )
            else:
                await _execute(
                    maintenance,
                    render_sql(
                        t"DROP LOGIN {ident(app_user)}",
                        dialect="mssql",
                        quote_identifier=quote_mssql_identifier,
                    ),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed SQL Server application login: {app_user}",
                    )
                )
            return tuple(results)
        finally:
            await maintenance.close()

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_mssql_context(context)
        return _MSSQL_MAINTENANCE_TASKS

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_mssql_context(context)
        task = request.task.strip()
        if task not in {
            maintenance_task.name for maintenance_task in _MSSQL_MAINTENANCE_TASKS
        }:
            raise DatabaseProvisioningConfigurationError(
                f"Unknown mssql maintenance task: {request.task}."
            )

        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(
            context,
            phase=f"maintenance:{task}",
        )
        target_database = _target_database(runtime_connection)
        app_user = _required_credential(
            runtime_connection,
            "user",
            "SQL Server maintenance requires a runtime application database user.",
        )
        schema = _schema_name(runtime_connection)
        role = _role_name(runtime_connection)

        if task == "prerequisites":
            return await self._validate_prerequisites(
                service_connection,
                app_user=app_user,
                app_password=_optional_credential(runtime_connection, "password"),
            )

        target = await self._connect(service_connection, database=target_database)
        try:
            if task == REPAIR_PRIVILEGES_TASK.name:
                await _repair_privileges(
                    target,
                    schema=schema,
                    role=role,
                    app_user=app_user,
                )
                return (
                    _result(
                        "maintenance",
                        "updated",
                        f"Repaired SQL Server runtime privileges for role: {role}",
                    ),
                )
            if task == TORTOISE_MIGRATIONS_TASK.name:
                return (
                    await _migration_state_result(
                        target,
                        schema,
                        phase="maintenance",
                    ),
                )
            raise DatabaseProvisioningConfigurationError(
                f"Unknown mssql maintenance task: {request.task}."
            )
        finally:
            await target.close()

    def quote_identifier(self, identifier: str) -> str:
        return quote_mssql_identifier(identifier)

    async def _validate_prerequisites(
        self,
        service_connection: ResolvedDatabaseConnection,
        *,
        app_user: str,
        app_password: str | None,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        service_database = _service_account_database(service_connection)
        maintenance = await self._connect(service_connection, database=service_database)
        try:
            login_exists = await _login_exists(maintenance, app_user)
        finally:
            await maintenance.close()

        if app_password is None and not login_exists:
            return (
                _external_login_prerequisite_result(
                    app_user,
                    phase="maintenance",
                ),
            )
        if app_password is None:
            return (_external_login_ready_result(app_user, phase="maintenance"),)
        return (
            _result(
                "maintenance",
                "noop",
                (
                    f"SQL Server managed application login exists: {app_user}"
                    if login_exists
                    else (
                        "SQL Server managed application login will be created "
                        f"during init: {app_user}"
                    )
                ),
            ),
        )

    async def _connect(
        self,
        connection: ResolvedDatabaseConnection,
        *,
        database: str,
    ) -> SQLServerConnection:
        credentials = dict(connection.credentials)
        credentials["database"] = database
        try:
            return await self._connector(credentials)
        except DatabaseProvisioningConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseProvisioningOperationError(
                f"Failed to connect to SQL Server database: {database}"
            ) from exc


class _DriverSQLServerConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def execute(self, query: str, *args: object) -> object:
        cursor = await self._cursor()
        try:
            return await cursor.execute(query, *args)
        finally:
            await _close_cursor(cursor)

    async def fetchval(self, query: str, *args: object) -> object:
        cursor = await self._cursor()
        try:
            await cursor.execute(query, *args)
            row = await cursor.fetchone()
        finally:
            await _close_cursor(cursor)
        if row is None:
            return None
        if isinstance(row, Mapping):
            return next(iter(row.values()), None)
        return cast(Any, row)[0]

    async def close(self) -> object:
        result = self._connection.close()
        if inspect.isawaitable(result):
            await result
        return None

    async def _cursor(self) -> SQLServerCursor:
        cursor = self._connection.cursor()
        if inspect.isawaitable(cursor):
            cursor = await cursor
        return cast(SQLServerCursor, cursor)


async def _close_cursor(cursor: SQLServerCursor) -> None:
    result = cursor.close()
    if inspect.isawaitable(result):
        await result


async def _connect_asyncodbc(
    credentials: Mapping[str, object],
) -> SQLServerConnection:
    try:
        asyncodbc = importlib.import_module("asyncodbc")
        importlib.import_module("pyodbc")
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise DatabaseProvisioningConfigurationError(
            "SQL Server provisioning requires the wybra[mssql] optional dependency."
        ) from exc

    connect = cast(Callable[..., Awaitable[object]], asyncodbc.connect)
    return _DriverSQLServerConnection(await connect(**_driver_credentials(credentials)))


def _driver_credentials(credentials: Mapping[str, object]) -> dict[str, object]:
    if credentials.get("dsn") is not None:
        driver_credentials = dict(credentials)
        driver_credentials.setdefault("autocommit", True)
        return driver_credentials

    driver = str(credentials.get("driver") or "ODBC Driver 18 for SQL Server")
    server = str(credentials.get("host") or "localhost")
    if credentials.get("port") is not None:
        server = f"{server},{credentials['port']}"

    connection_parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={_odbc_value(server)}",
    ]
    if credentials.get("database") is not None:
        connection_parts.append(f"DATABASE={_odbc_value(credentials['database'])}")
    if credentials.get("user") is not None:
        connection_parts.append(f"UID={_odbc_value(credentials['user'])}")
    if credentials.get("password") is not None:
        connection_parts.append(f"PWD={_odbc_value(credentials['password'])}")
    for key, value in credentials.items():
        if key in {"database", "driver", "host", "password", "port", "user"}:
            continue
        attribute = _normalise_odbc_attribute(key)
        connection_parts.append(f"{attribute}={_odbc_value(value)}")

    return {
        "dsn": ";".join(connection_parts),
        "autocommit": True,
    }


def _normalise_odbc_attribute(key: object) -> str:
    if not isinstance(key, str) or not key.strip():
        raise DatabaseProvisioningConfigurationError(
            "SQL Server ODBC attribute names must be non-blank strings."
        )
    normalised = key.strip().replace("_", "").replace(" ", "").casefold()
    if normalised not in _ALLOWED_ODBC_ATTRIBUTE_KEYS:
        raise DatabaseProvisioningConfigurationError(
            f"Unsupported SQL Server ODBC attribute: {key}."
        )
    return _ALLOWED_ODBC_ATTRIBUTE_KEYS[normalised]


def _odbc_value(value: object) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


async def _ensure_database(
    connection: SQLServerConnection,
    database: str,
) -> bool:
    database_exists = await _database_exists(connection, database)
    if database_exists:
        return False
    await _execute(
        connection,
        render_sql(
            t"CREATE DATABASE {ident(database)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return True


async def _database_exists(connection: SQLServerConnection, database: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.databases WHERE name = {param(database)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _ensure_login(
    connection: SQLServerConnection,
    *,
    app_user: str,
    app_password: str,
) -> bool:
    login_exists = await _login_exists(connection, app_user)
    if login_exists:
        return False
    await _execute(
        connection,
        render_sql(
            t"DECLARE @sql nvarchar(max) = N'CREATE LOGIN ' + "
            t"QUOTENAME({param(app_user)}) + "
            t"N' WITH PASSWORD = N''' + "
            t"REPLACE({param(app_password)}, N'''', N'''''') + N''''; "
            t"EXEC(@sql)",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return True


async def _login_exists(connection: SQLServerConnection, app_user: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.server_principals "
            t"WHERE name = {param(app_user)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _ensure_schema(connection: SQLServerConnection, schema: str) -> bool:
    schema_exists = await _schema_exists(connection, schema)
    if schema_exists:
        return False
    await _execute(
        connection,
        render_sql(
            t"CREATE SCHEMA {ident(schema)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return True


async def _schema_exists(connection: SQLServerConnection, schema: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.schemas WHERE name = {param(schema)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _ensure_database_user(
    connection: SQLServerConnection,
    *,
    app_user: str,
    schema: str,
) -> bool:
    user_exists = await _database_user_exists(connection, app_user)
    if user_exists:
        await _execute(
            connection,
            render_sql(
                t"ALTER USER {ident(app_user)} WITH LOGIN = {ident(app_user)}",
                dialect="mssql",
                quote_identifier=quote_mssql_identifier,
            ),
        )
        await _execute(
            connection,
            render_sql(
                t"ALTER USER {ident(app_user)} WITH DEFAULT_SCHEMA = {ident(schema)}",
                dialect="mssql",
                quote_identifier=quote_mssql_identifier,
            ),
        )
        return False
    await _execute(
        connection,
        render_sql(
            t"CREATE USER {ident(app_user)} FOR LOGIN {ident(app_user)} "
            t"WITH DEFAULT_SCHEMA = {ident(schema)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return True


async def _database_user_exists(
    connection: SQLServerConnection,
    app_user: str,
) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.database_principals "
            t"WHERE name = {param(app_user)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _ensure_role(connection: SQLServerConnection, role: str) -> bool:
    role_exists = await _role_exists(connection, role)
    if role_exists:
        return False
    await _execute(
        connection,
        render_sql(
            t"CREATE ROLE {ident(role)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return True


async def _role_exists(connection: SQLServerConnection, role: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.database_principals "
            t"WHERE name = {param(role)} AND type = N'R'",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _repair_privileges(
    connection: SQLServerConnection,
    *,
    schema: str,
    role: str,
    app_user: str,
) -> None:
    if not await _role_member_exists(connection, role=role, app_user=app_user):
        await _execute(
            connection,
            render_sql(
                t"ALTER ROLE {ident(role)} ADD MEMBER {ident(app_user)}",
                dialect="mssql",
                quote_identifier=quote_mssql_identifier,
            ),
        )
    await _execute(
        connection,
        render_sql(
            t"GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::"
            t"{ident(schema)} TO {ident(role)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )


async def _role_member_exists(
    connection: SQLServerConnection,
    *,
    role: str,
    app_user: str,
) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.database_role_members drm "
            t"JOIN sys.database_principals role_principal "
            t"ON role_principal.principal_id = drm.role_principal_id "
            t"JOIN sys.database_principals member_principal "
            t"ON member_principal.principal_id = drm.member_principal_id "
            t"WHERE role_principal.name = {param(role)} "
            t"AND member_principal.name = {param(app_user)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _migration_state_result(
    connection: SQLServerConnection,
    schema: str,
    *,
    phase: ProvisioningPhase = "init",
) -> ProvisioningPhaseResult:
    table_count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.tables table_info "
            t"JOIN sys.schemas schema_info "
            t"ON schema_info.schema_id = table_info.schema_id "
            t"WHERE schema_info.name = {param(schema)} "
            t"AND table_info.name = {param(_MIGRATION_RECORDER_TABLE)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    if _normalise_count(table_count) == 0:
        return _result(
            phase,
            "noop",
            "Tortoise migration recorder table is absent.",
        )

    migration_count = _normalise_count(
        await _fetchval(
            connection,
            render_sql(
                t"SELECT COUNT(*) FROM "
                t"{ident(schema)}.{ident(_MIGRATION_RECORDER_TABLE)}",
                dialect="mssql",
                quote_identifier=quote_mssql_identifier,
            ),
        )
    )
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


async def _terminate_database_sessions(
    connection: SQLServerConnection,
    database: str,
) -> None:
    await _execute(
        connection,
        render_sql(
            t"ALTER DATABASE {ident(database)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )


async def _login_has_external_dependencies(
    connection: SQLServerConnection,
    *,
    app_user: str,
) -> bool:
    server_role_count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.server_role_members role_members "
            t"JOIN sys.server_principals member_principal "
            t"ON member_principal.principal_id = role_members.member_principal_id "
            t"WHERE member_principal.name = {param(app_user)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    if _normalise_count(server_role_count) > 0:
        return True

    permission_count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM sys.server_permissions "
            t"WHERE grantee_principal_id = SUSER_ID({param(app_user)}) "
            t"AND permission_name <> {param(_BASELINE_SERVER_PERMISSION)}",
            dialect="mssql",
            quote_identifier=quote_mssql_identifier,
        ),
    )
    return _normalise_count(permission_count) > 0


async def _execute(connection: SQLServerConnection, statement: RenderedSql) -> object:
    try:
        return await connection.execute(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "SQL Server provisioning statement failed."
        ) from exc


async def _fetchval(
    connection: SQLServerConnection,
    statement: RenderedSql,
) -> object:
    try:
        return await connection.fetchval(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "SQL Server provisioning query failed."
        ) from exc


def _ensure_mssql_context(context: ProvisioningContext) -> None:
    if context.family != "mssql":
        raise DatabaseProvisioningConfigurationError(
            f"Provisioner mssql cannot handle database family {context.family}."
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
        "SQL Server lifecycle requires a target database.",
    )


def _service_account_database(connection: ResolvedDatabaseConnection) -> str:
    if connection.sa_database is not None:
        return connection.sa_database
    return _DEFAULT_SERVICE_ACCOUNT_DATABASE


def _schema_name(connection: ResolvedDatabaseConnection) -> str:
    value = connection.credentials.get("schema")
    if value is None:
        return _DEFAULT_SCHEMA
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DatabaseProvisioningConfigurationError(
        "SQL Server schema must be a non-blank string."
    )


def _role_name(connection: ResolvedDatabaseConnection) -> str:
    value = connection.credentials.get("role")
    if value is None:
        return _DEFAULT_ROLE
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DatabaseProvisioningConfigurationError(
        "SQL Server role must be a non-blank string."
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
        f"SQL Server {name} credential must be a non-blank string."
    )


def _ensure_lifecycle_database_is_distinct(
    target_database: str,
    service_database: str,
) -> None:
    if target_database.casefold() == service_database.casefold():
        raise DatabaseProvisioningConfigurationError(
            "SQL Server target database must differ from the service-account database."
        )


def _ensure_destroy_confirmed(
    target_database: str,
    service_database: str,
    request: DestroyDatabaseRequest,
) -> None:
    _ensure_lifecycle_database_is_distinct(target_database, service_database)
    if request.confirm.strip() != target_database:
        raise DatabaseProvisioningConfigurationError(
            "SQL Server destroy confirmation does not match the configured database."
        )


def _external_login_prerequisite_result(
    app_user: str,
    *,
    phase: ProvisioningPhase = "init",
) -> ProvisioningPhaseResult:
    return _result(
        phase,
        "noop",
        "SQL Server application login requires an externally managed "
        f"password or prerequisite setup: {app_user}",
    )


def _external_login_ready_result(
    app_user: str,
    *,
    phase: ProvisioningPhase = "init",
) -> ProvisioningPhaseResult:
    return _result(
        phase,
        "noop",
        f"SQL Server application login is externally managed: {app_user}",
    )


def quote_mssql_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("SQL identifier must not be blank.")
    return "[" + identifier.strip().replace("]", "]]") + "]"


def _normalise_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise DatabaseProvisioningOperationError("SQL Server count result was invalid.")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise DatabaseProvisioningOperationError("SQL Server count result was invalid.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseProvisioningOperationError(
            "SQL Server count result was invalid."
        ) from exc


def _result(
    phase: ProvisioningPhase,
    status: ProvisioningStatus,
    message: str,
) -> ProvisioningPhaseResult:
    return ProvisioningPhaseResult(
        family="mssql",
        phase=phase,
        status=status,
        message=message,
    )


__all__ = ("SQLServerProvisioner", "quote_mssql_identifier")
