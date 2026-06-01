from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import click
import dateparser
from sqlalchemy import Table
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from auth_ext.configuration import ConfigurationError
from auth_ext.database import close_database, create_database, session_scope
from auth_ext.management import (
    ERROR_FINAL_SUPERUSER,
    ERROR_INVALID_TIMEZONE,
    ERROR_INVALID_USER_ID,
    ERROR_NO_CHANGES,
    ERROR_NOT_FOUND,
    ERROR_SUPERUSER_PROTECTED,
    ERROR_UNSUPPORTED_ORDER,
    USER_RECORD_FIELDS,
    USER_TIMESTAMP_FIELDS,
    create_local_user_for_management,
    deactivate_local_user_for_management,
    delete_local_user_for_management,
    list_local_users_for_management,
    resolve_user_target,
    target_error_message,
    update_local_user_for_management,
    user_record,
)
from auth_ext.models import User
from auth_ext.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
)
from auth_ext.settings import load_auth_settings

TIMESTAMP_FIELDS: frozenset[str] = frozenset(USER_TIMESTAMP_FIELDS)
PasswordSource = Literal["-", "prompt"]
PASSWORD_SOURCE_STDIN: PasswordSource = "-"
PASSWORD_SOURCE_PROMPT: PasswordSource = "prompt"
PASSWORD_SOURCE_STDIN_ALIAS = "stdin"
TIMESTAMP_HELP = (
    "Timestamp options parse numeric input as Unix seconds before date parsing; "
    "use separated calendar forms such as 2025-01-01 for dates."
)
SCHEMA_MIGRATION_MESSAGE = (
    "Auth database schema is not up to date; run `uv run migrate upgrade` for "
    "the configured database. If using an explicit auth database, run "
    "`uv run migrate --database-url <auth-database-url> upgrade`."
)
SCHEMA_INSPECTION_MESSAGE = (
    "Auth database schema could not be inspected; verify database connectivity, "
    "permissions, and locks."
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UsermgrArgs:
    command: str
    config: Path | None = None
    email: str = ""
    target: str = ""
    password: PasswordSource | None = None
    admin: bool = False
    superuser: bool = False
    unverified: bool = False
    is_admin: bool | None = None
    is_superuser: bool | None = None
    is_verified: bool | None = None
    no_revoke: bool = False
    display_name: str | None = None
    clear_display_name: bool = False
    preferred_name: str | None = None
    clear_preferred_name: bool = False
    preferred_timezone: str | None = None
    clear_preferred_timezone: bool = False
    expires_at: float | None = None
    no_expires_at: bool = False
    force: bool = False
    json_output: bool = False
    csv_output: bool = False
    email_pattern: str | None = None
    domain_pattern: str | None = None
    effective_active: bool | None = None
    since_created_at: float | None = None
    before_created_at: float | None = None
    since_modified_at: float | None = None
    before_modified_at: float | None = None
    since_last_login_at: float | None = None
    before_last_login_at: float | None = None
    never_logged_in: bool | None = None
    order: str = "email"
    direction: str | None = None


@dataclass(frozen=True, slots=True)
class IdentitySchemaStatus:
    table_name: str
    table_exists: bool
    missing_columns: tuple[str, ...]


class PasswordSourceError(Exception):
    """Raised when a password source cannot produce a usable password."""


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _password_source_option(default: PasswordSource | None):
    """Build the shared password-source option.

    ``default=PASSWORD_SOURCE_PROMPT`` means the command requires a password and
    omitted ``--password`` still prompts. ``default=None`` means password input is
    optional; only bare ``--password`` prompts and ``--password -`` reads stdin.
    """

    return click.option(
        "--password",
        is_flag=False,
        flag_value=PASSWORD_SOURCE_PROMPT,
        default=default,
        callback=_password_source_callback,
        metavar="[SOURCE]",
        help="Password source. Omit the value for a hidden prompt, or use '-'/'stdin'.",
    )


def _password_source_callback(
    _ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> PasswordSource | None:
    """Accept only the supported password-source sentinels.

    ``None`` is preserved for optional password-update flows where omitting the
    option means "leave the password unchanged".
    """

    if value is None:
        return value

    if value in {PASSWORD_SOURCE_STDIN, PASSWORD_SOURCE_STDIN_ALIAS}:
        return PASSWORD_SOURCE_STDIN

    if value == PASSWORD_SOURCE_PROMPT:
        return PASSWORD_SOURCE_PROMPT

    raise click.BadParameter(
        "must be '-' or omitted, or one of: stdin, prompt",
        param=param,
    )


def _timestamp_callback(
    _ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> float | None:
    if value is None:
        return None

    try:
        return parse_timestamp_filter(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param=param) from exc


def _optional_boolean(
    enabled: bool,
    disabled: bool,
    *,
    positive: str,
    negative: str,
) -> bool | None:
    _ensure_mutually_exclusive((enabled, positive), (disabled, negative))
    if enabled:
        return True
    if disabled:
        return False
    return None


def _ensure_mutually_exclusive(*options: tuple[object, str]) -> None:
    selected = [
        option_name for value, option_name in options if _option_was_provided(value)
    ]
    if len(selected) > 1:
        first, second = selected[:2]
        raise click.UsageError(
            f"Option '{second}' is not allowed with option '{first}'."
        )


def _option_was_provided(value: object) -> bool:
    """Return whether a Click option value represents an explicit selection.

    Click boolean flags use the ``False`` singleton for omitted flags. Other
    falsy values such as ``0`` or ``""`` are explicit option values and must
    still participate in mutual-exclusion checks.
    """

    return value is not None and value is not False


def _config_path(ctx: click.Context) -> Path | None:
    """Return the auth config path from Click context without hiding bad state.

    Unexpected context state is treated as a usage error so operators see a
    normal CLI failure instead of an internal exception.
    """

    config = ctx.obj.get("config") if ctx.obj else None
    if config is None:
        return None
    if isinstance(config, Path):
        return config
    if isinstance(config, str):
        return Path(config)
    raise click.UsageError(
        f"Invalid type for --config: expected path or string, got {type(config)!r}."
    )


def _run_usermgr(ctx: click.Context, args: UsermgrArgs) -> None:
    try:
        exit_code = asyncio.run(_main_async(args))
    except PasswordSourceError as exc:
        raise click.BadParameter(str(exc), param_hint=["--password"]) from exc
    except ConfigurationError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        exit_code = 1
    except click.Abort:
        raise
    ctx.exit(exit_code)


@click.group(
    name="usermgr",
    context_settings=CONTEXT_SETTINGS,
    epilog=TIMESTAMP_HELP,
    help="Manage local identity users through configured services.",
)
@click.option(
    "--config",
    type=click.Path(path_type=Path),
    help="Path to auth.toml. Defaults to AUTH_CONFIG or ./auth.toml when present.",
)
@click.pass_context
def usermgr_command(ctx: click.Context, config: Path | None) -> None:
    ctx.obj = {"config": config}


@usermgr_command.command("create", help="Create a local user.")
@click.argument("email")
@_password_source_option(default=PASSWORD_SOURCE_PROMPT)
@click.option("--admin", is_flag=True)
@click.option("--superuser", is_flag=True)
@click.option("--unverified", is_flag=True)
@click.option("--display-name")
@click.option("--preferred-name")
@click.option("--timezone", "preferred_timezone")
@click.option("--expires-at", callback=_timestamp_callback)
@click.pass_context
def create_command(
    ctx: click.Context,
    email: str,
    password: PasswordSource,
    admin: bool,
    superuser: bool,
    unverified: bool,
    display_name: str | None,
    preferred_name: str | None,
    preferred_timezone: str | None,
    expires_at: float | None,
) -> None:
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="create",
            config=_config_path(ctx),
            email=email,
            password=password,
            admin=admin,
            superuser=superuser,
            unverified=unverified,
            display_name=display_name,
            preferred_name=preferred_name,
            preferred_timezone=preferred_timezone,
            expires_at=expires_at,
        ),
    )


@usermgr_command.command("update", help="Update a local user.")
@click.argument("target")
@click.option("--admin", "admin", is_flag=True)
@click.option("--no-admin", "no_admin", is_flag=True)
@click.option("--superuser", "superuser", is_flag=True)
@click.option("--no-superuser", "no_superuser", is_flag=True)
@click.option("--verify", "verify", is_flag=True)
@click.option("--no-verify", "no_verify", is_flag=True)
@_password_source_option(default=None)
@click.option("--no-revoke", is_flag=True)
@click.option("--display-name")
@click.option("--no-display-name", "clear_display_name", is_flag=True)
@click.option("--preferred-name")
@click.option("--no-preferred-name", "clear_preferred_name", is_flag=True)
@click.option("--timezone", "preferred_timezone")
@click.option("--no-timezone", "clear_preferred_timezone", is_flag=True)
@click.option("--expires-at", callback=_timestamp_callback)
@click.option("--no-expires-at", is_flag=True)
@click.pass_context
def update_command(
    ctx: click.Context,
    target: str,
    admin: bool,
    no_admin: bool,
    superuser: bool,
    no_superuser: bool,
    verify: bool,
    no_verify: bool,
    password: PasswordSource | None,
    no_revoke: bool,
    display_name: str | None,
    clear_display_name: bool,
    preferred_name: str | None,
    clear_preferred_name: bool,
    preferred_timezone: str | None,
    clear_preferred_timezone: bool,
    expires_at: float | None,
    no_expires_at: bool,
) -> None:
    _ensure_mutually_exclusive(
        (display_name, "--display-name"), (clear_display_name, "--no-display-name")
    )
    _ensure_mutually_exclusive(
        (preferred_name, "--preferred-name"),
        (clear_preferred_name, "--no-preferred-name"),
    )
    _ensure_mutually_exclusive(
        (preferred_timezone, "--timezone"), (clear_preferred_timezone, "--no-timezone")
    )
    _ensure_mutually_exclusive(
        (expires_at, "--expires-at"), (no_expires_at, "--no-expires-at")
    )
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="update",
            config=_config_path(ctx),
            target=target,
            is_admin=_optional_boolean(
                admin,
                no_admin,
                positive="--admin",
                negative="--no-admin",
            ),
            is_superuser=_optional_boolean(
                superuser,
                no_superuser,
                positive="--superuser",
                negative="--no-superuser",
            ),
            is_verified=_optional_boolean(
                verify,
                no_verify,
                positive="--verify",
                negative="--no-verify",
            ),
            password=password,
            no_revoke=no_revoke,
            display_name=display_name,
            clear_display_name=clear_display_name,
            preferred_name=preferred_name,
            clear_preferred_name=clear_preferred_name,
            preferred_timezone=preferred_timezone,
            clear_preferred_timezone=clear_preferred_timezone,
            expires_at=expires_at,
            no_expires_at=no_expires_at,
        ),
    )


@usermgr_command.command("delete", help="Delete a local user.")
@click.argument("target")
@click.option("--force", is_flag=True)
@click.pass_context
def delete_command(ctx: click.Context, target: str, force: bool) -> None:
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="delete",
            config=_config_path(ctx),
            target=target,
            force=force,
        ),
    )


@usermgr_command.command("deactivate", help="Deactivate a local user.")
@click.argument("target")
@click.option("--force", is_flag=True)
@click.pass_context
def deactivate_command(ctx: click.Context, target: str, force: bool) -> None:
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="deactivate",
            config=_config_path(ctx),
            target=target,
            force=force,
        ),
    )


@usermgr_command.command("list", help="List local users.")
@click.option("--json", "json_output", is_flag=True)
@click.option("--csv", "csv_output", is_flag=True)
@click.option("--email", "-e", "email_pattern")
@click.option("--domain", "-d", "domain_pattern")
@click.option("--admin", "admin", is_flag=True)
@click.option("--non-admin", "non_admin", is_flag=True)
@click.option("--superuser", "superuser", is_flag=True)
@click.option("--non-superuser", "non_superuser", is_flag=True)
@click.option("--active", "active", is_flag=True)
@click.option("--inactive", "inactive", is_flag=True)
@click.option("--verified", "verified", is_flag=True)
@click.option("--unverified", "unverified", is_flag=True)
@click.option("--since-created-at", "-C", callback=_timestamp_callback)
@click.option("--before-created-at", "-c", callback=_timestamp_callback)
@click.option("--since-modified-at", "-M", callback=_timestamp_callback)
@click.option("--before-modified-at", "-m", callback=_timestamp_callback)
@click.option("--since-last-login-at", "-L", callback=_timestamp_callback)
@click.option("--before-last-login-at", "-l", callback=_timestamp_callback)
@click.option("--never-logged-in", is_flag=True)
@click.option("--logged-in", is_flag=True)
@click.option(
    "--order",
    type=click.Choice(
        ("email", "email-domain", "created-at", "modified-at", "last-login-at")
    ),
    default="email",
    show_default=True,
    help=(
        "Sort field. Timestamp fields default to most-recent-first unless "
        "--direction is set."
    ),
)
@click.option(
    "--direction",
    type=click.Choice(("asc", "desc")),
    help=(
        "Sort direction. Defaults to asc for email fields and desc for "
        "timestamp fields."
    ),
)
@click.pass_context
def list_command(
    ctx: click.Context,
    json_output: bool,
    csv_output: bool,
    email_pattern: str | None,
    domain_pattern: str | None,
    admin: bool,
    non_admin: bool,
    superuser: bool,
    non_superuser: bool,
    active: bool,
    inactive: bool,
    verified: bool,
    unverified: bool,
    since_created_at: float | None,
    before_created_at: float | None,
    since_modified_at: float | None,
    before_modified_at: float | None,
    since_last_login_at: float | None,
    before_last_login_at: float | None,
    never_logged_in: bool,
    logged_in: bool,
    order: str,
    direction: str | None,
) -> None:
    _ensure_mutually_exclusive((json_output, "--json"), (csv_output, "--csv"))
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="list",
            config=_config_path(ctx),
            json_output=json_output,
            csv_output=csv_output,
            email_pattern=email_pattern,
            domain_pattern=domain_pattern,
            is_admin=_optional_boolean(
                admin,
                non_admin,
                positive="--admin",
                negative="--non-admin",
            ),
            is_superuser=_optional_boolean(
                superuser,
                non_superuser,
                positive="--superuser",
                negative="--non-superuser",
            ),
            effective_active=_optional_boolean(
                active,
                inactive,
                positive="--active",
                negative="--inactive",
            ),
            is_verified=_optional_boolean(
                verified,
                unverified,
                positive="--verified",
                negative="--unverified",
            ),
            since_created_at=since_created_at,
            before_created_at=before_created_at,
            since_modified_at=since_modified_at,
            before_modified_at=before_modified_at,
            since_last_login_at=since_last_login_at,
            before_last_login_at=before_last_login_at,
            never_logged_in=_optional_boolean(
                never_logged_in,
                logged_in,
                positive="--never-logged-in",
                negative="--logged-in",
            ),
            order=order,
            direction=direction,
        ),
    )


@usermgr_command.command("password", help="Change a local user's password.")
@click.argument("target")
@_password_source_option(default=PASSWORD_SOURCE_PROMPT)
@click.option("--no-revoke", is_flag=True)
@click.pass_context
def password_command(
    ctx: click.Context,
    target: str,
    password: PasswordSource,
    no_revoke: bool,
) -> None:
    _run_usermgr(
        ctx,
        UsermgrArgs(
            command="password",
            config=_config_path(ctx),
            target=target,
            password=password,
            no_revoke=no_revoke,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = usermgr_command.main(
            args=None if argv is None else list(argv),
            prog_name="usermgr",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.Abort:
        print("Aborted!", file=sys.stderr)
        return 1
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


async def _main_async(args: UsermgrArgs) -> int:
    settings = load_auth_settings(config_path=args.config)
    database = create_database(settings.database_url)
    try:
        async with session_scope(database.session_factory) as session:
            await _verify_identity_schema(session)
            match args.command:
                case "create":
                    password = _read_required_password(args.password)
                    result = await create_local_user_for_management(
                        session,
                        settings.identity_options,
                        email=args.email,
                        password=password,
                        is_admin=args.admin,
                        is_superuser=args.superuser,
                        is_verified=not args.unverified,
                        display_name=args.display_name,
                        preferred_name=args.preferred_name,
                        preferred_timezone=args.preferred_timezone,
                        expires_at=args.expires_at,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"created user: {value.get('email', args.email)}")
                    return 0
                case "update":
                    password = (
                        _read_password(args.password)
                        if args.password is not None
                        else None
                    )
                    result = await update_local_user_for_management(
                        session,
                        settings.identity_options,
                        target=args.target,
                        is_admin=args.is_admin,
                        is_superuser=args.is_superuser,
                        is_verified=args.is_verified,
                        password=password,
                        revoke_sessions=not args.no_revoke,
                        display_name=args.display_name,
                        clear_display_name=args.clear_display_name,
                        preferred_name=args.preferred_name,
                        clear_preferred_name=args.clear_preferred_name,
                        preferred_timezone=args.preferred_timezone,
                        clear_preferred_timezone=args.clear_preferred_timezone,
                        expires_at=args.expires_at,
                        clear_expires_at=args.no_expires_at,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"updated user: {value.get('email', args.target)}")
                    return 0
                case "delete":
                    if not args.force:
                        target_result = await _resolve_target_record(
                            session, args.target
                        )
                        if target_result.is_failure():
                            return _print_failure(
                                target_result.error_type,
                                target_result.message,
                            )
                        target_record = target_result.value or {}
                        if not _confirm_destructive("delete", target_record):
                            return 1

                    result = await delete_local_user_for_management(
                        session,
                        target=args.target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deleted user: {value.get('email', args.target)}")
                    return 0
                case "deactivate":
                    if not args.force:
                        target_result = await _resolve_target_record(
                            session, args.target
                        )
                        if target_result.is_failure():
                            return _print_failure(
                                target_result.error_type,
                                target_result.message,
                            )
                        target_record = target_result.value or {}
                        if not _confirm_destructive("deactivate", target_record):
                            return 1

                    result = await deactivate_local_user_for_management(
                        session,
                        target=args.target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deactivated user: {value.get('email', args.target)}")
                    return 0
                case "list":
                    result = await list_local_users_for_management(
                        session,
                        email_pattern=args.email_pattern,
                        domain_pattern=args.domain_pattern,
                        is_admin=args.is_admin,
                        is_superuser=args.is_superuser,
                        effective_active=args.effective_active,
                        is_verified=args.is_verified,
                        since_created_at=args.since_created_at,
                        before_created_at=args.before_created_at,
                        since_modified_at=args.since_modified_at,
                        before_modified_at=args.before_modified_at,
                        since_last_login_at=args.since_last_login_at,
                        before_last_login_at=args.before_last_login_at,
                        never_logged_in=args.never_logged_in,
                        order=args.order,
                        direction=args.direction,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    records = (result.value or {}).get("users", [])
                    _print_user_records(
                        records,
                        json_output=args.json_output,
                        csv_output=args.csv_output,
                    )
                    return 0
                case "password":
                    password = _read_required_password(args.password)
                    result = await update_local_user_for_management(
                        session,
                        settings.identity_options,
                        target=args.target,
                        password=password,
                        revoke_sessions=not args.no_revoke,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"changed password: {value.get('email', args.target)}")
                    return 0
                case _:
                    print(f"{args.command}: not implemented", file=sys.stderr)
                    return 1
    finally:
        await close_database(database)


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
            f"{SCHEMA_MIGRATION_MESSAGE} Missing {table_name} columns: "
            f"{', '.join(schema_status.missing_columns)}."
        )


def _identity_table_qualified_name() -> str:
    user_table = cast(Table, User.__table__)
    if user_table.schema:
        return f"{user_table.schema}.{user_table.name}"
    return user_table.name


def _identity_schema_status(session: Session) -> IdentitySchemaStatus:
    inspector = sqlalchemy_inspect(session.get_bind())
    user_table = cast(Table, User.__table__)
    table_name = user_table.name
    schema = user_table.schema
    expected_column_names = tuple(str(column.name) for column in user_table.columns)
    expected_columns = {
        _normalise_identifier(column_name): column_name
        for column_name in expected_column_names
    }
    if not inspector.has_table(table_name, schema=schema):
        return IdentitySchemaStatus(
            table_name=table_name,
            table_exists=False,
            missing_columns=(),
        )

    database_columns = {
        _normalise_identifier(column["name"])
        for column in inspector.get_columns(table_name, schema=schema)
    }
    missing_columns = (
        column_name
        for normalised_name, column_name in expected_columns.items()
        if normalised_name not in database_columns
    )
    return IdentitySchemaStatus(
        table_name=table_name,
        table_exists=True,
        missing_columns=tuple(sorted(missing_columns)),
    )


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


def _read_password(value: PasswordSource) -> str:
    match value:
        case "-":
            if sys.stdin.isatty():
                raise PasswordSourceError(
                    "Refusing to read password from interactive stdin; "
                    "pipe a password or omit --password for a hidden prompt."
                )
            line = sys.stdin.readline()
            if line == "":
                raise PasswordSourceError("No password received on stdin.")

            password = line.rstrip("\r\n")
            if sys.stdin.read(1):
                raise PasswordSourceError(
                    "Password stdin input must contain exactly one line."
                )
            return password
        case "prompt":
            return click.prompt(
                "Password",
                hide_input=True,
                confirmation_prompt=True,
                err=True,
            )
        case _:
            raise PasswordSourceError(f"Unsupported password source: {value!r}")


def _read_required_password(value: PasswordSource | None) -> str:
    if value is None:
        value = PASSWORD_SOURCE_PROMPT
    return _read_password(value)


def parse_timestamp_filter(value: str) -> float:
    """Parse CLI timestamp input.

    Numeric input is intentionally interpreted first as Unix seconds. Use a
    separated date form such as ``2025-01-01`` for calendar dates.
    """

    try:
        return float(value)
    except ValueError:
        pass

    parsed = dateparser.parse(
        value,
        settings=_timestamp_parser_settings(),
    )
    if parsed is None:
        raise ValueError(f"Invalid timestamp value: {value}")

    return parsed.astimezone(UTC).timestamp()


def _timestamp_parser_settings() -> dict[str, object]:
    return {
        "DATE_ORDER": "DMY",
        "DEFAULT_LANGUAGES": ["en"],
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": _local_timezone_name(),
        "TO_TIMEZONE": "UTC",
    }


def _local_timezone_name() -> str:
    local_timezone = datetime.now().astimezone().tzinfo
    return _timezone_name_from_tzinfo(local_timezone)


def _timezone_name_from_tzinfo(local_timezone: Any) -> str:
    if local_timezone is None:
        return "UTC"

    timezone_name = getattr(local_timezone, "key", None)
    if isinstance(timezone_name, str) and timezone_name:
        return timezone_name

    timezone_name = getattr(local_timezone, "zone", None)
    if isinstance(timezone_name, str) and timezone_name:
        return timezone_name

    return "UTC"


async def _resolve_target_record(
    session,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))

    if user is None:
        return Result.failure(ERROR_NOT_FOUND)

    return Result.ok(user_record(user))


def _confirm_destructive(action: str, record: dict[str, Any]) -> bool:
    answer = input(
        f"{action} {record['email']} ({record['id']})? Type 'yes' to continue: "
    )
    return answer == "yes"


def _print_failure(error_type: str | None, message: str | None) -> int:
    fallback_messages = {
        ERROR_ALREADY_EXISTS: "User already exists.",
        ERROR_FINAL_SUPERUSER: "Cannot remove the final superuser flag.",
        ERROR_INVALID_EMAIL: "Email address is invalid.",
        ERROR_INVALID_PASSWORD: "Password is invalid.",
        ERROR_INVALID_TIMEZONE: "Preferred timezone is invalid.",
        ERROR_INVALID_USER_ID: "User target must be an email address or valid user ID.",
        ERROR_NO_CHANGES: "No user changes were requested.",
        ERROR_NOT_FOUND: "No matching user was found.",
        ERROR_SUPERUSER_PROTECTED: "Superuser accounts are protected.",
        ERROR_UNSUPPORTED_ORDER: "Requested ordering is not supported.",
    }
    fallback_message = (
        fallback_messages.get(error_type) if error_type is not None else None
    )
    print(
        message or fallback_message or "User management failed.",
        file=sys.stderr,
    )
    return 1


def _print_user_records(
    records: list[dict[str, Any]],
    *,
    json_output: bool,
    csv_output: bool,
) -> None:
    cleaned_records = [_record_without_nulls(record) for record in records]
    if json_output:
        # Contract: JSON output omits unset optional fields instead of emitting nulls.
        print(json.dumps(cleaned_records))
        return

    if csv_output:
        writer = csv.DictWriter(sys.stdout, fieldnames=_csv_fieldnames())
        writer.writeheader()
        writer.writerows(_records_for_human_output(cleaned_records))
        return

    for record in _records_for_human_output(cleaned_records):
        print(
            " ".join(
                [
                    str(record.get("email", "<unknown>")),
                    f"id={record.get('id', '<unknown>')}",
                    f"admin={record.get('is_admin', False)}",
                    f"superuser={record.get('is_superuser', False)}",
                    f"active={record.get('effective_active', False)}",
                    f"verified={record.get('is_verified', False)}",
                ]
            )
        )


def _record_without_nulls(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field_name: record[field_name]
        for field_name in USER_RECORD_FIELDS
        if field_name in record and record[field_name] is not None
    }


def _records_for_human_output(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            field_name: _format_human_value(field_name, record.get(field_name))
            for field_name in USER_RECORD_FIELDS
            if field_name in record
        }
        for record in records
    ]


def _format_human_value(field_name: str, value: Any) -> Any:
    if field_name in TIMESTAMP_FIELDS and isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except OverflowError:
            return value
        except OSError:
            return value
        except ValueError:
            return value

    return value


def _csv_fieldnames() -> list[str]:
    return list(USER_RECORD_FIELDS)
