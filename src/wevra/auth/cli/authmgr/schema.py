from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from wevra.auth.models import (
    Group,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityUserEmail,
    Scope,
    User,
)
from wevra.core.exceptions import ConfigurationError

SCHEMA_MIGRATION_MESSAGE = (
    "Auth database schema is not up to date; run `uv run wevra-migrate init` "
    "for first-time database provisioning and migration-state setup, then "
    "`uv run wevra-migrate upgrade` to apply schema migrations from the host "
    "app project, or set APP_CONFIG to the same app.toml used by wevra-authmgr. "
    "If deliberately overriding the application database, run "
    "`uv run wevra-migrate --database-url <database-url> init` followed by "
    "`uv run wevra-migrate --database-url <database-url> upgrade`."
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


async def _verify_identity_schema(session: AsyncSession) -> None:
    try:
        schema_status = await session.run_sync(_identity_schema_status)
    except SQLAlchemyError as exc:
        logger.warning(
            "Auth database schema inspection failed.",
            extra={
                "table_name": User.__tablename__,
                "schema": getattr(User.__table__, "schema", None),
            },
        )
        _log_schema_inspection_error(exc)
        raise ConfigurationError(SCHEMA_INSPECTION_MESSAGE) from exc

    table_name = _identity_table_qualified_name()
    if not schema_status.table_exists:
        raise ConfigurationError(
            f"{SCHEMA_MIGRATION_MESSAGE} Missing {table_name} table."
        )

    if schema_status.missing_columns:
        raise ConfigurationError(
            f"{SCHEMA_MIGRATION_MESSAGE} Missing identity schema columns: "
            f"{', '.join(schema_status.missing_columns)}."
        )

    if schema_status.missing_tables:
        raise ConfigurationError(
            f"{SCHEMA_MIGRATION_MESSAGE} "
            f"{_missing_tables_message(schema_status.missing_tables)}"
        )


def _identity_table_qualified_name() -> str:
    user_table = cast(Table, User.__table__)
    if user_table.schema:
        return f"{user_table.schema}.{user_table.name}"
    return user_table.name


def _identity_schema_status(session: Session) -> IdentitySchemaStatus:
    inspector = sqlalchemy_inspect(session.get_bind())
    user_table = _identity_schema_tables()[0]
    if not inspector.has_table(user_table.name, schema=user_table.schema):
        return IdentitySchemaStatus(
            primary_table_name=user_table.name,
            table_exists=False,
            missing_columns=(),
        )

    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for table in _identity_schema_tables():
        if not inspector.has_table(table.name, schema=table.schema):
            missing_tables.append(_qualified_table_name(table))
            continue

        missing_columns.extend(_missing_schema_columns(inspector, table))

    return IdentitySchemaStatus(
        primary_table_name=user_table.name,
        table_exists=True,
        missing_columns=tuple(sorted(missing_columns)),
        missing_tables=tuple(sorted(missing_tables)),
    )


def _identity_schema_tables() -> tuple[Table, ...]:
    return (
        cast(Table, User.__table__),
        cast(Table, Group.__table__),
        cast(Table, Scope.__table__),
        cast(Table, GroupScope.__table__),
        cast(Table, GroupUser.__table__),
        cast(Table, GroupGroup.__table__),
        cast(Table, IdentityUserEmail.__table__),
    )


def _missing_schema_columns(inspector: Any, table: Table) -> list[str]:
    expected_column_names = tuple(str(column.name) for column in table.columns)
    expected_columns = {
        _normalise_identifier(column_name): column_name
        for column_name in expected_column_names
    }
    database_columns = {
        _normalise_identifier(column["name"])
        for column in inspector.get_columns(table.name, schema=table.schema)
    }
    return [
        _qualified_column_name(table, column_name)
        for normalised_name, column_name in expected_columns.items()
        if normalised_name not in database_columns
    ]


def _qualified_table_name(table: Table) -> str:
    if table.schema:
        return f"{table.schema}.{table.name}"
    return table.name


def _qualified_column_name(table: Table, column_name: str) -> str:
    user_table = cast(Table, User.__table__)
    if table is user_table:
        return column_name
    return f"{_qualified_table_name(table)}.{column_name}"


def _missing_tables_message(table_names: tuple[str, ...]) -> str:
    return " ".join(f"Missing {table_name} table." for table_name in table_names)


def _normalise_identifier(value: object) -> str:
    return str(value).casefold()


def _log_schema_inspection_error(exc: SQLAlchemyError) -> None:
    logger.debug(
        "Failed to inspect identity_user schema.",
        exc_info=True,
        extra={
            "table_name": User.__tablename__,
            "schema": getattr(User.__table__, "schema", None),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        },
    )
