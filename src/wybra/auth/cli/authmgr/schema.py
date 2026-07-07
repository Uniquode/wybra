from __future__ import annotations

import logging
from dataclasses import dataclass

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import BaseORMException
from tortoise.models import Model

from wybra.auth.models import (
    Group,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityUserEmail,
    Scope,
    User,
)
from wybra.core.exceptions import ConfigurationError
from wybra.db.persistence import Database

SCHEMA_MIGRATION_MESSAGE = (
    "Auth database schema is not up to date; run `uv run wybra-migrate init` "
    "for first-time database provisioning and migration-state setup, then "
    "`uv run wybra-migrate migrate` to apply schema migrations from the host "
    "app project with the same selected app config used by wybra-authmgr. "
    "If deliberately overriding the application database, run "
    "`uv run wybra-migrate --database-url <database-url> init` followed by "
    "`uv run wybra-migrate --database-url <database-url> migrate`."
)
SCHEMA_INSPECTION_MESSAGE = (
    "Auth database schema could not be inspected; verify database connectivity, "
    "permissions, and locks."
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdentitySchemaStatus:
    primary_table_name: str
    table_exists: bool
    missing_columns: tuple[str, ...]
    missing_tables: tuple[str, ...] = ()


async def verify_identity_schema_for_database(database: Database) -> None:
    await _verify_identity_schema(database.connection())


async def _verify_identity_schema(connection: BaseDBAsyncClient) -> None:
    try:
        schema_status = await _identity_schema_status(connection)
    except BaseORMException as exc:
        logger.warning(
            "Auth database schema inspection failed.",
            extra={"table_name": _model_table_name(User)},
        )
        _log_schema_inspection_error(exc)
        raise ConfigurationError(SCHEMA_INSPECTION_MESSAGE) from exc

    table_name = _model_table_name(User)
    if not schema_status.table_exists:
        raise ConfigurationError(
            f"{SCHEMA_MIGRATION_MESSAGE} Missing {table_name} table."
        )

    schema_messages: list[str] = []
    if schema_status.missing_tables:
        schema_messages.append(
            f"Missing identity schema tables: "
            f"{_missing_tables_message(schema_status.missing_tables)}"
        )
    if schema_status.missing_columns:
        schema_messages.append(
            "Missing identity schema columns: "
            f"{', '.join(schema_status.missing_columns)}."
        )
    if schema_messages:
        raise ConfigurationError(
            f"{SCHEMA_MIGRATION_MESSAGE}\n" + "\n".join(schema_messages)
        )


async def _identity_schema_status(
    connection: BaseDBAsyncClient,
) -> IdentitySchemaStatus:
    table_columns = await _database_table_columns(connection)
    user_table = _identity_schema_models()[0]
    user_table_name = _model_table_name(user_table)
    if user_table_name not in table_columns:
        return IdentitySchemaStatus(
            primary_table_name=user_table_name,
            table_exists=False,
            missing_columns=(),
        )

    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for model in _identity_schema_models():
        table_name = _model_table_name(model)
        database_columns = table_columns.get(table_name)
        if database_columns is None:
            missing_tables.append(table_name)
            continue

        missing_columns.extend(_missing_schema_columns(model, database_columns))

    return IdentitySchemaStatus(
        primary_table_name=user_table_name,
        table_exists=True,
        missing_columns=tuple(sorted(missing_columns)),
        missing_tables=tuple(sorted(missing_tables)),
    )


def _identity_schema_models() -> tuple[type[Model], ...]:
    return (
        User,
        Group,
        Scope,
        GroupScope,
        GroupUser,
        GroupGroup,
        IdentityUserEmail,
    )


async def _database_table_columns(
    connection: BaseDBAsyncClient,
) -> dict[str, set[str]]:
    dialect = str(getattr(connection.capabilities, "dialect", "")).casefold()
    if dialect == "sqlite":
        return await _sqlite_table_columns(connection)
    if dialect == "postgres":
        return await _postgres_table_columns(connection)
    raise ConfigurationError(
        f"{SCHEMA_INSPECTION_MESSAGE} Unsupported database dialect: {dialect}."
    )


async def _sqlite_table_columns(
    connection: BaseDBAsyncClient,
) -> dict[str, set[str]]:
    tables = await connection.execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    table_columns: dict[str, set[str]] = {}
    for row in tables:
        table_name = str(row["name"])
        columns = await connection.execute_query_dict(
            f"PRAGMA table_info({_quote_sqlite_identifier(table_name)})"
        )
        table_columns[table_name] = {
            _normalise_identifier(column["name"]) for column in columns
        }
    return table_columns


async def _postgres_table_columns(
    connection: BaseDBAsyncClient,
) -> dict[str, set[str]]:
    rows = await connection.execute_query_dict(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
        """
    )
    table_columns: dict[str, set[str]] = {}
    for row in rows:
        table_columns.setdefault(str(row["table_name"]), set()).add(
            _normalise_identifier(row["column_name"])
        )
    return table_columns


def _missing_schema_columns(
    model: type[Model],
    database_columns: set[str],
) -> list[str]:
    expected_columns = {
        _normalise_identifier(column_name): column_name
        for column_name in _model_column_names(model)
    }
    return [
        _qualified_column_name(model, column_name)
        for normalised_name, column_name in expected_columns.items()
        if normalised_name not in database_columns
    ]


def _model_column_names(model: type[Model]) -> tuple[str, ...]:
    relation_fields = {*model._meta.fk_fields, *model._meta.o2o_fields}
    return tuple(
        str(field.source_field or field_name)
        for field_name, field in model._meta.fields_map.items()
        if field_name not in relation_fields
    )


def _model_table_name(model: type[Model]) -> str:
    return str(model._meta.db_table)


def _qualified_column_name(model: type[Model], column_name: str) -> str:
    if model is User:
        return column_name
    return f"{_model_table_name(model)}.{column_name}"


def _missing_tables_message(table_names: tuple[str, ...]) -> str:
    return " ".join(f"Missing {table_name} table." for table_name in table_names)


def _normalise_identifier(value: object) -> str:
    return str(value).casefold()


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _log_schema_inspection_error(exc: BaseORMException) -> None:
    logger.debug(
        "Failed to inspect identity_user schema.",
        exc_info=True,
        extra={
            "table_name": _model_table_name(User),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        },
    )
