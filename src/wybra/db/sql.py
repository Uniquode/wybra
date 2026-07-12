from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from string.templatelib import Interpolation, Template
from typing import Literal

SqlDialect = Literal["postgresql", "sqlite"]


@dataclass(frozen=True, slots=True)
class SqlIdentifier:
    value: str


@dataclass(frozen=True, slots=True)
class SqlParameter:
    value: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrustedSql:
    value: str


@dataclass(frozen=True, slots=True)
class RenderedSql:
    statement: str
    parameters: tuple[object, ...] = field(default=(), repr=False)


def ident(value: str) -> SqlIdentifier:
    return SqlIdentifier(value)


def param(value: object) -> SqlParameter:
    return SqlParameter(value)


def trusted_sql(value: str) -> TrustedSql:
    if not isinstance(value, str) or not value:
        raise ValueError("Trusted SQL fragment must not be blank.")
    return TrustedSql(value)


def quote_sql_identifier(identifier: str) -> str:
    """Quote an SQL identifier using the standard double-quote form."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("SQL identifier must not be blank.")
    return '"' + identifier.strip().replace('"', '""') + '"'


def render_sql(
    template: Template,
    *,
    dialect: SqlDialect,
    quote_identifier: Callable[[str], str] = quote_sql_identifier,
) -> RenderedSql:
    if not isinstance(template, Template):
        raise TypeError("SQL rendering requires a Python t-string Template.")

    statement_parts: list[str] = []
    parameters: list[object] = []
    for part in template:
        if isinstance(part, str):
            statement_parts.append(part)
            continue
        if isinstance(part, Interpolation):
            statement_parts.append(
                _render_interpolation(
                    part,
                    dialect=dialect,
                    parameter_index=len(parameters) + 1,
                    quote_identifier=quote_identifier,
                )
            )
            if isinstance(part.value, SqlParameter):
                parameters.append(part.value.value)
            continue
        raise TypeError(f"Unsupported SQL template part: {type(part).__name__}.")

    return RenderedSql(
        statement="".join(statement_parts),
        parameters=tuple(parameters),
    )


def _render_interpolation(
    interpolation: Interpolation,
    *,
    dialect: SqlDialect,
    parameter_index: int,
    quote_identifier: Callable[[str], str],
) -> str:
    if interpolation.conversion is not None or interpolation.format_spec:
        raise ValueError("SQL template interpolations must not use formatting.")

    value = interpolation.value
    if isinstance(value, SqlIdentifier):
        return quote_identifier(value.value)
    if isinstance(value, SqlParameter):
        return _parameter_marker(dialect, parameter_index)
    if isinstance(value, TrustedSql):
        return value.value
    raise TypeError(
        "SQL template interpolation must use ident(), param(), or trusted_sql(): "
        f"{interpolation.expression}."
    )


def _parameter_marker(dialect: SqlDialect, index: int) -> str:
    if dialect == "sqlite":
        return "?"
    if dialect == "postgresql":
        return f"${index}"
    raise ValueError(f"Unsupported SQL dialect: {dialect}.")


__all__ = (
    "RenderedSql",
    "SqlDialect",
    "SqlIdentifier",
    "SqlParameter",
    "TrustedSql",
    "ident",
    "param",
    "quote_sql_identifier",
    "render_sql",
    "trusted_sql",
)
