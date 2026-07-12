from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

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
from wybra.db.sql import RenderedSql, ident, param, render_sql, trusted_sql

_MIGRATION_RECORDER_TABLE = "tortoise_migrations"
_MYSQL_ACCOUNT_HOST = "%"
_MYSQL_MAINTENANCE_TASKS = (
    DatabaseMaintenanceTask(
        name="repair-privileges",
        description="Reapply runtime user database grants.",
        recommended_frequency="after migrations or user changes",
    ),
    DatabaseMaintenanceTask(
        name="migration-state",
        description="Report Tortoise migration recorder state.",
    ),
)


class MySQLConnection(Protocol):
    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchval(self, query: str, *args: object) -> object: ...

    async def fetchall(
        self, query: str, *args: object
    ) -> tuple[tuple[object, ...], ...]: ...

    async def close(self) -> object: ...


MySQLConnector = Callable[
    [Mapping[str, object]],
    Awaitable[MySQLConnection],
]


class MySQLProvisioner:
    family: DatabaseFamily = "mysql"

    def __init__(self, connector: MySQLConnector | None = None) -> None:
        self._connector = connector or _connect_driver_mysql

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_mysql_context(context)
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="init")
        target_database = _target_database(runtime_connection)
        app_user = _required_credential(
            runtime_connection,
            "user",
            "MySQL init requires a runtime application database user.",
        )
        app_password = _required_credential(
            runtime_connection,
            "password",
            "MySQL init requires a runtime application database password.",
        )

        maintenance = await self._connect(service_connection)
        try:
            results: list[ProvisioningPhaseResult] = []
            database_created = await _ensure_database(maintenance, target_database)
            results.append(
                _result(
                    "init",
                    "created" if database_created else "skipped",
                    (
                        f"Created MySQL database: {target_database}"
                        if database_created
                        else f"MySQL database already exists: {target_database}"
                    ),
                )
            )
            user_created = await _ensure_application_user(
                maintenance,
                app_user=app_user,
                app_password=app_password,
            )
            results.append(
                _result(
                    "init",
                    "created" if user_created else "skipped",
                    (
                        f"Created MySQL application user: {app_user}"
                        if user_created
                        else f"MySQL application user already exists: {app_user}"
                    ),
                )
            )
            await _repair_privileges(
                maintenance,
                database=target_database,
                app_user=app_user,
            )
            results.append(
                _result(
                    "init",
                    "skipped",
                    f"Repaired MySQL runtime privileges for user: {app_user}",
                )
            )
            results.append(await _migration_state_result(maintenance, target_database))
            return tuple(results)
        finally:
            await maintenance.close()

    async def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_mysql_context(context)
        runtime_connection = context.runtime_connection
        service_connection = _service_account_connection(context, phase="destroy")
        target_database = _target_database(runtime_connection)
        service_user = _required_credential(
            service_connection,
            "user",
            "MySQL destroy requires a service-account database user.",
        )
        app_user = _required_credential(
            runtime_connection,
            "user",
            "MySQL destroy requires a runtime application database user.",
        )
        _ensure_destroy_confirmed(target_database, request)

        maintenance = await self._connect(service_connection)
        try:
            results: list[ProvisioningPhaseResult] = []
            database_exists = await _database_exists(maintenance, target_database)
            user_exists = await _user_exists(maintenance, app_user)
            user_has_external_grants = (
                user_exists
                and app_user != service_user
                and await _user_has_external_grants(
                    maintenance,
                    app_user=app_user,
                    target_database=target_database,
                )
            )
            if database_exists:
                await _terminate_database_sessions(maintenance, target_database)
                await _execute(
                    maintenance,
                    render_sql(
                        t"DROP DATABASE {ident(target_database)}",
                        dialect="mysql",
                        quote_identifier=quote_mysql_identifier,
                    ),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed MySQL database: {target_database}",
                    )
                )
            else:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"MySQL database already absent: {target_database}",
                    )
                )

            if not user_exists:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        f"MySQL application user already absent: {app_user}",
                    )
                )
            elif app_user == service_user:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped MySQL application user removal because runtime "
                        "and service-account users are the same.",
                    )
                )
            elif user_has_external_grants:
                results.append(
                    _result(
                        "destroy",
                        "skipped",
                        "Skipped MySQL application user removal because the user "
                        f"has grants outside {target_database}.",
                    )
                )
            else:
                await _execute(
                    maintenance,
                    render_sql(
                        t"DROP USER {trusted_sql(_account_name(app_user))}",
                        dialect="mysql",
                        quote_identifier=quote_mysql_identifier,
                    ),
                )
                results.append(
                    _result(
                        "destroy",
                        "removed",
                        f"Removed MySQL application user: {app_user}",
                    )
                )
            return tuple(results)
        finally:
            await maintenance.close()

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_mysql_context(context)
        return _MYSQL_MAINTENANCE_TASKS

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_mysql_context(context)
        task = request.task.strip()
        if task not in {
            maintenance_task.name for maintenance_task in _MYSQL_MAINTENANCE_TASKS
        }:
            raise DatabaseProvisioningConfigurationError(
                f"Unknown mysql maintenance task: {request.task}."
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
            "MySQL maintenance requires a runtime application database user.",
        )

        maintenance = await self._connect(service_connection)
        try:
            if task == "repair-privileges":
                await _repair_privileges(
                    maintenance,
                    database=target_database,
                    app_user=app_user,
                )
                return (
                    _result(
                        "maintenance",
                        "skipped",
                        f"Repaired MySQL runtime privileges for user: {app_user}",
                    ),
                )
            return (
                await _migration_state_result(
                    maintenance,
                    target_database,
                    phase="maintenance",
                ),
            )
        finally:
            await maintenance.close()

    def quote_identifier(self, identifier: str) -> str:
        return quote_mysql_identifier(identifier)

    async def _connect(
        self,
        connection: ResolvedDatabaseConnection,
    ) -> MySQLConnection:
        credentials = dict(connection.credentials)
        credentials.pop("database", None)
        credentials.pop("db", None)
        try:
            return await self._connector(credentials)
        except DatabaseProvisioningConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseProvisioningOperationError(
                "Failed to connect to MySQL server for database lifecycle."
            ) from exc


class _DriverMySQLConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def execute(self, query: str, *args: object) -> object:
        async with self._connection.cursor() as cursor:
            return await cursor.execute(query, args or None)

    async def fetchval(self, query: str, *args: object) -> object:
        async with self._connection.cursor() as cursor:
            await cursor.execute(query, args or None)
            row = await cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, Mapping):
            return next(iter(row.values()), None)
        return row[0]

    async def fetchall(
        self, query: str, *args: object
    ) -> tuple[tuple[object, ...], ...]:
        async with self._connection.cursor() as cursor:
            await cursor.execute(query, args or None)
            rows = await cursor.fetchall()
        return tuple(_normalise_row(row) for row in rows)

    async def close(self) -> object:
        result = self._connection.close()
        if inspect.isawaitable(result):
            await result
        wait_closed = getattr(self._connection, "wait_closed", None)
        if callable(wait_closed):
            wait_result = wait_closed()
            if inspect.isawaitable(wait_result):
                await wait_result
        return None


async def _connect_driver_mysql(credentials: Mapping[str, object]) -> MySQLConnection:
    import_errors: list[ImportError] = []
    for module_name in ("asyncmy", "aiomysql"):
        try:
            mysql = importlib.import_module(module_name)
        except ImportError as exc:  # pragma: no cover - depends on installed extras
            import_errors.append(exc)
            continue

        connect = cast(Callable[..., Awaitable[object]], mysql.connect)
        driver_credentials = _driver_credentials(credentials)
        return _DriverMySQLConnection(await connect(**driver_credentials))

    cause = import_errors[-1] if import_errors else None
    raise DatabaseProvisioningConfigurationError(
        "MySQL provisioning requires the wybra[mysql] optional dependency."
    ) from cause


def _driver_credentials(credentials: Mapping[str, object]) -> dict[str, object]:
    driver_credentials = dict(credentials)
    database = driver_credentials.pop("database", None)
    driver_credentials.pop("db", None)
    if database is not None:
        driver_credentials["db"] = database
    driver_credentials.setdefault("autocommit", True)
    return driver_credentials


async def _ensure_database(connection: MySQLConnection, database: str) -> bool:
    database_exists = await _database_exists(connection, database)
    if database_exists:
        return False
    await _execute(
        connection,
        render_sql(
            t"CREATE DATABASE {ident(database)}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )
    return True


async def _database_exists(connection: MySQLConnection, database: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA "
            t"WHERE SCHEMA_NAME = {param(database)}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _ensure_application_user(
    connection: MySQLConnection,
    *,
    app_user: str,
    app_password: str,
) -> bool:
    account = _account_name(app_user, escape_percent=True)
    if await _user_exists(connection, app_user):
        await _execute(
            connection,
            render_sql(
                t"ALTER USER {trusted_sql(account)} "
                t"IDENTIFIED BY {param(app_password)}",
                dialect="mysql",
                quote_identifier=quote_mysql_identifier,
            ),
        )
        return False

    await _execute(
        connection,
        render_sql(
            t"CREATE USER {trusted_sql(account)} IDENTIFIED BY {param(app_password)}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )
    return True


async def _user_exists(connection: MySQLConnection, app_user: str) -> bool:
    count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM mysql.user "
            t"WHERE User = {param(app_user)} AND Host = {param(_MYSQL_ACCOUNT_HOST)}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )
    return _normalise_count(count) > 0


async def _repair_privileges(
    connection: MySQLConnection,
    *,
    database: str,
    app_user: str,
) -> None:
    await _execute(
        connection,
        render_sql(
            t"GRANT SELECT, INSERT, UPDATE, DELETE ON {ident(database)}.* "
            t"TO {trusted_sql(_account_name(app_user))}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )


async def _terminate_database_sessions(
    connection: MySQLConnection,
    database: str,
) -> None:
    try:
        current_connection_id = _normalise_integer(
            await _fetchval(
                connection,
                render_sql(
                    t"SELECT CONNECTION_ID()",
                    dialect="mysql",
                    quote_identifier=quote_mysql_identifier,
                ),
            ),
            message="MySQL connection id was invalid.",
        )
        rows = await _fetchall(
            connection,
            render_sql(
                t"SELECT ID FROM INFORMATION_SCHEMA.PROCESSLIST "
                t"WHERE DB = {param(database)} "
                t"AND ID <> {param(current_connection_id)}",
                dialect="mysql",
                quote_identifier=quote_mysql_identifier,
            ),
        )
        for row in rows:
            if not row:
                continue
            process_id = _normalise_integer(
                row[0],
                message="MySQL process id was invalid.",
            )
            await _execute(
                connection,
                render_sql(
                    t"KILL {trusted_sql(str(process_id))}",
                    dialect="mysql",
                    quote_identifier=quote_mysql_identifier,
                ),
            )
    except DatabaseProvisioningOperationError as exc:
        raise DatabaseProvisioningOperationError(
            "Failed to terminate MySQL sessions for target database."
        ) from exc


async def _migration_state_result(
    connection: MySQLConnection,
    database: str,
    *,
    phase: ProvisioningPhase = "init",
) -> ProvisioningPhaseResult:
    table_count = await _fetchval(
        connection,
        render_sql(
            t"SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            t"WHERE TABLE_SCHEMA = {param(database)} "
            t"AND TABLE_NAME = {param(_MIGRATION_RECORDER_TABLE)}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
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
                t"{ident(database)}.{ident(_MIGRATION_RECORDER_TABLE)}",
                dialect="mysql",
                quote_identifier=quote_mysql_identifier,
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


async def _user_has_external_grants(
    connection: MySQLConnection,
    *,
    app_user: str,
    target_database: str,
) -> bool:
    rows = await _fetchall(
        connection,
        render_sql(
            t"SHOW GRANTS FOR {trusted_sql(_account_name(app_user))}",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        ),
    )
    grants = tuple(str(row[0]) for row in rows if row)
    return any(
        _grant_has_external_scope(grant, target_database=target_database)
        for grant in grants
    )


def _grant_has_external_scope(grant: str, *, target_database: str) -> bool:
    scope_match = re.search(r"\bON\s+(.+?)\s+TO\b", grant, flags=re.IGNORECASE)
    if scope_match is None:
        return True

    scope = scope_match.group(1).strip()
    if scope == "*.*":
        return not grant.upper().startswith("GRANT USAGE ON *.*")

    return not scope.startswith(f"{quote_mysql_identifier(target_database)}.")


async def _execute(connection: MySQLConnection, statement: RenderedSql) -> object:
    try:
        return await connection.execute(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "MySQL provisioning statement failed."
        ) from exc


async def _fetchval(connection: MySQLConnection, statement: RenderedSql) -> object:
    try:
        return await connection.fetchval(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "MySQL provisioning query failed."
        ) from exc


async def _fetchall(
    connection: MySQLConnection,
    statement: RenderedSql,
) -> tuple[tuple[object, ...], ...]:
    try:
        return await connection.fetchall(statement.statement, *statement.parameters)
    except Exception as exc:
        raise DatabaseProvisioningOperationError(
            "MySQL provisioning query failed."
        ) from exc


def _ensure_mysql_context(context: ProvisioningContext) -> None:
    if context.family != "mysql":
        raise DatabaseProvisioningConfigurationError(
            f"Provisioner mysql cannot handle database family {context.family}."
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
        "MySQL lifecycle requires a target database.",
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


def _ensure_destroy_confirmed(
    target_database: str,
    request: DestroyDatabaseRequest,
) -> None:
    if request.confirm.strip() != target_database:
        raise DatabaseProvisioningConfigurationError(
            "MySQL destroy confirmation does not match the configured database."
        )


def quote_mysql_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("SQL identifier must not be blank.")
    return "`" + identifier.strip().replace("`", "``") + "`"


def _account_name(user: str, *, escape_percent: bool = False) -> str:
    host = (
        _MYSQL_ACCOUNT_HOST.replace("%", "%%")
        if escape_percent
        else _MYSQL_ACCOUNT_HOST
    )
    return f"{_mysql_string_literal(user)}@{_mysql_string_literal(host)}"


def _mysql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _normalise_count(value: object) -> int:
    return _normalise_integer(
        value,
        message="MySQL migration recorder count was invalid.",
    )


def _normalise_integer(value: object, *, message: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise DatabaseProvisioningOperationError(message)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise DatabaseProvisioningOperationError(message)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseProvisioningOperationError(message) from exc


def _normalise_row(row: object) -> tuple[object, ...]:
    if isinstance(row, Mapping):
        return tuple(row.values())
    return tuple(cast(Any, row))


def _result(
    phase: ProvisioningPhase,
    status: ProvisioningStatus,
    message: str,
) -> ProvisioningPhaseResult:
    return ProvisioningPhaseResult(
        family="mysql",
        phase=phase,
        status=status,
        message=message,
    )


__all__ = ("MySQLProvisioner", "quote_mysql_identifier")
