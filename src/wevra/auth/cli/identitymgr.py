from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

import click
import dateparser
from sqlalchemy import Table, delete, select
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from wevra.auth.admin.management import (
    ERROR_CYCLIC_GROUP_MEMBERSHIP,
    ERROR_FINAL_SUPERUSER,
    ERROR_GROUP_HAS_MEMBERSHIPS,
    ERROR_INVALID_GROUP_ID,
    ERROR_INVALID_TIMEZONE,
    ERROR_INVALID_USER_ID,
    ERROR_NO_CHANGES,
    ERROR_NOT_FOUND,
    ERROR_SCOPE_IN_USE,
    ERROR_SUPERUSER_PROTECTED,
    ERROR_UNSUPPORTED_ORDER,
    USER_RECORD_FIELDS,
    USER_TIMESTAMP_FIELDS,
    add_child_group_to_group_for_management,
    add_scope_to_group_for_management,
    add_user_to_group_for_management,
    create_group_for_management,
    create_local_user_for_management,
    create_scope_for_management,
    deactivate_local_user_for_management,
    delete_group_for_management,
    delete_local_user_for_management,
    delete_scope_for_management,
    effective_scopes_for_user_for_management,
    get_group_for_management,
    group_target_error_message,
    list_groups_for_management,
    list_local_users_for_management,
    list_scopes_for_management,
    remove_child_group_from_group_for_management,
    remove_scope_from_group_for_management,
    remove_user_from_group_for_management,
    resolve_user_target,
    target_error_message,
    update_group_for_management,
    update_local_user_for_management,
    update_scope_for_management,
    user_record,
)
from wevra.auth.configuration import ConfigurationError
from wevra.auth.models import Group, GroupGroup, GroupScope, GroupUser, Scope, User
from wevra.auth.persistence.database import (
    close_database,
    create_database,
    session_scope,
)
from wevra.auth.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
)
from wevra.auth.settings import AuthSettings, load_auth_settings
from wevra.core.composition import CompositionError, load_app_config
from wevra.tools.project import ProjectToolConfigurationError, runtime_project_root

TIMESTAMP_FIELDS: frozenset[str] = frozenset(USER_TIMESTAMP_FIELDS)
PROGRAM_NAME = "wevra-authmgr"
PasswordSource = Literal["-", "prompt"]
PASSWORD_SOURCE_STDIN: PasswordSource = "-"
PASSWORD_SOURCE_PROMPT: PasswordSource = "prompt"
PASSWORD_SOURCE_STDIN_ALIAS = "stdin"
TIMESTAMP_HELP = (
    "Timestamp options parse numeric input as Unix seconds before date parsing; "
    "use separated calendar forms such as 2025-01-01 for dates."
)
SCHEMA_MIGRATION_MESSAGE = (
    "Auth database schema is not up to date; run `uv run wevra-migrate "
    "upgrade` from the host app project, or set APP_CONFIG to the same "
    "app.toml used by wevra-authmgr. If deliberately overriding the application "
    "database, run `uv run wevra-migrate --database-url <database-url> upgrade`."
)
SCHEMA_INSPECTION_MESSAGE = (
    "Auth database schema could not be inspected; verify database connectivity, "
    "permissions, and locks."
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IdentitymgrArgs:
    command: str
    email: str = ""
    target: str = ""
    group_target: str = ""
    child_group_target: str = ""
    user_target: str = ""
    scope: str = ""
    description: str | None = None
    add_scopes: tuple[str, ...] = ()
    remove_scopes: tuple[str, ...] = ()
    add_groups: tuple[str, ...] = ()
    remove_groups: tuple[str, ...] = ()
    set_groups: tuple[str, ...] = ()
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
    primary_table_name: str
    table_exists: bool
    missing_columns: tuple[str, ...]
    missing_tables: tuple[str, ...] = ()


class PasswordSourceError(Exception):
    """Raised when a password source cannot produce a usable password."""


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


class HelpSuffixGroup(click.Group):
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and args[0] == "help":
            if len(args) == 1:
                click.echo(ctx.get_help(), color=ctx.color)
                ctx.exit()
            args = self._help_path_args(ctx, args[1:])
        return super().resolve_command(ctx, args)

    def _help_path_args(self, ctx: click.Context, path: list[str]) -> list[str]:
        command_name = path[0]
        command = self.get_command(ctx, command_name)
        if command is None:
            raise click.UsageError(f"No such command '{command_name}'.")
        if isinstance(command, click.Group):
            return [command_name, "help", *path[1:]]
        if _accepts_raw_help_path(command):
            return [command_name, "help", *path[1:]]
        if len(path) == 1:
            return [command_name, "--help"]
        raise click.UsageError(f"Nested help is not available for '{' '.join(path)}'.")


def _accepts_raw_help_path(command: click.Command) -> bool:
    context_settings = command.context_settings or {}
    return bool(
        context_settings.get("allow_extra_args")
        and context_settings.get("ignore_unknown_options")
    )


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


def _run_identitymgr(ctx: click.Context, args: IdentitymgrArgs) -> None:
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


def _group_args(ctx: click.Context, tokens: tuple[str, ...]) -> IdentitymgrArgs:
    if not tokens:
        raise click.UsageError("Missing group operation.")

    operation = tokens[0]
    match operation:
        case "create":
            parsed = _parse_cli_tokens(
                tokens[1:],
                value_options={"--description", "--scope"},
                flag_options=set(),
            )
            if len(parsed.positionals) != 1:
                raise click.UsageError("Usage: wevra-authmgr group create <abbrev>.")
            return IdentitymgrArgs(
                command="group-create",
                group_target=parsed.positionals[0],
                description=parsed.single_option("--description"),
                add_scopes=tuple(parsed.option_values("--scope")),
            )
        case "list":
            parsed = _parse_cli_tokens(
                tokens[1:],
                value_options=set(),
                flag_options={"--json", "--csv"},
            )
            if parsed.positionals:
                raise click.UsageError(
                    "Usage: wevra-authmgr group list [--json|--csv]."
                )
            _ensure_mutually_exclusive(
                (parsed.has_flag("--json"), "--json"),
                (parsed.has_flag("--csv"), "--csv"),
            )
            return IdentitymgrArgs(
                command="group-list",
                json_output=parsed.has_flag("--json"),
                csv_output=parsed.has_flag("--csv"),
            )
        case "effective-scopes":
            parsed = _parse_cli_tokens(
                tokens[1:],
                value_options=set(),
                flag_options={"--json"},
            )
            if len(parsed.positionals) != 1:
                raise click.UsageError(
                    "Usage: wevra-authmgr group effective-scopes <user-target>."
                )
            return IdentitymgrArgs(
                command="group-effective-scopes",
                user_target=parsed.positionals[0],
                json_output=parsed.has_flag("--json"),
            )
        case _:
            return _target_group_args(ctx, tokens)


def _target_group_args(ctx: click.Context, tokens: tuple[str, ...]) -> IdentitymgrArgs:
    if len(tokens) < 2:
        raise click.UsageError("Missing group target operation.")

    target, operation, *remaining = tokens
    match operation:
        case "show":
            parsed = _parse_cli_tokens(
                remaining,
                value_options=set(),
                flag_options={"--json"},
            )
            if parsed.positionals:
                raise click.UsageError(
                    "Usage: wevra-authmgr group <group> show [--json]."
                )
            return IdentitymgrArgs(
                command="group-show",
                group_target=target,
                json_output=parsed.has_flag("--json"),
            )
        case "update":
            parsed = _parse_cli_tokens(
                remaining,
                value_options={"--description", "--scope", "--rm-scope"},
                flag_options=set(),
            )
            if parsed.positionals:
                raise click.UsageError(
                    "Usage: wevra-authmgr group <group> update "
                    "[--description <text>] [--scope <scope>] [--rm-scope <scope>]."
                )
            return IdentitymgrArgs(
                command="group-update",
                group_target=target,
                description=parsed.single_option("--description"),
                add_scopes=tuple(parsed.option_values("--scope")),
                remove_scopes=tuple(parsed.option_values("--rm-scope")),
            )
        case "delete":
            parsed = _parse_cli_tokens(
                remaining,
                value_options=set(),
                flag_options={"--force"},
            )
            if parsed.positionals:
                raise click.UsageError(
                    "Usage: wevra-authmgr group <group> delete [--force]."
                )
            return IdentitymgrArgs(
                command="group-delete",
                group_target=target,
                force=parsed.has_flag("--force"),
            )
        case "add-user" | "remove-user":
            parsed = _parse_cli_tokens(
                remaining,
                value_options=set(),
                flag_options=set(),
            )
            if len(parsed.positionals) != 1:
                raise click.UsageError(
                    f"Usage: wevra-authmgr group <group> {operation} <user>."
                )
            return IdentitymgrArgs(
                command=f"group-{operation}",
                group_target=target,
                user_target=parsed.positionals[0],
            )
        case "add-group" | "remove-group":
            parsed = _parse_cli_tokens(
                remaining,
                value_options=set(),
                flag_options=set(),
            )
            if len(parsed.positionals) != 1:
                raise click.UsageError(
                    f"Usage: wevra-authmgr group <group> {operation} <group>."
                )
            return IdentitymgrArgs(
                command=f"group-{operation}",
                group_target=target,
                child_group_target=parsed.positionals[0],
            )
        case _:
            raise click.UsageError(f"Unknown group operation: {operation}.")


_GROUP_ROOT_OPERATION_HELP = {
    "create": (
        "Usage: wevra-authmgr group create <abbrev> "
        "[--description <text>] [--scope <scope>]."
    ),
    "list": "Usage: wevra-authmgr group list [--json|--csv].",
    "effective-scopes": (
        "Usage: wevra-authmgr group effective-scopes <user-target> [--json]."
    ),
}

_GROUP_TARGET_OPERATION_HELP = {
    "show": "Usage: wevra-authmgr group <group> show [--json].",
    "update": (
        "Usage: wevra-authmgr group <group> update "
        "[--description <text>] [--scope <scope>] [--rm-scope <scope>]."
    ),
    "delete": "Usage: wevra-authmgr group <group> delete [--force].",
    "add-user": "Usage: wevra-authmgr group <group> add-user <user>.",
    "remove-user": "Usage: wevra-authmgr group <group> remove-user <user>.",
    "add-group": "Usage: wevra-authmgr group <group> add-group <group>.",
    "remove-group": "Usage: wevra-authmgr group <group> remove-group <group>.",
}


def _group_operation_help(tokens: tuple[str, ...]) -> str:
    help_text: str | None = None
    if len(tokens) == 1:
        help_text = _GROUP_ROOT_OPERATION_HELP.get(
            tokens[0]
        ) or _GROUP_TARGET_OPERATION_HELP.get(tokens[0])
    if len(tokens) == 2:
        help_text = _GROUP_TARGET_OPERATION_HELP.get(tokens[1])
    if help_text is None:
        raise click.UsageError(
            f"Unknown group help topic: {' '.join(tokens)}. "
            "Try 'wevra-authmgr group --help'."
        )
    return help_text


@dataclass(frozen=True, slots=True)
class ParsedCliTokens:
    positionals: list[str]
    value_options: dict[str, list[str]]
    flags: set[str]

    def option_values(self, option_name: str) -> list[str]:
        return self.value_options.get(option_name, [])

    def single_option(self, option_name: str) -> str | None:
        values = self.option_values(option_name)
        if len(values) > 1:
            raise click.UsageError(f"Option {option_name} can only be provided once.")
        return values[0] if values else None

    def has_flag(self, option_name: str) -> bool:
        return option_name in self.flags


def _parse_cli_tokens(
    tokens: Sequence[str],
    *,
    value_options: set[str],
    flag_options: set[str],
) -> ParsedCliTokens:
    positionals: list[str] = []
    parsed_value_options: dict[str, list[str]] = {}
    parsed_flags: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in value_options:
            if index + 1 >= len(tokens):
                raise click.UsageError(f"Option {token} requires a value.")
            parsed_value_options.setdefault(token, []).append(tokens[index + 1])
            index += 2
            continue
        if token in flag_options:
            parsed_flags.add(token)
            index += 1
            continue
        if token.startswith("-"):
            raise click.UsageError(f"Unknown option: {token}.")
        positionals.append(token)
        index += 1

    return ParsedCliTokens(positionals, parsed_value_options, parsed_flags)


@click.group(
    name=PROGRAM_NAME,
    cls=HelpSuffixGroup,
    context_settings=CONTEXT_SETTINGS,
    epilog=TIMESTAMP_HELP,
    help="Manage local identity resources through configured services.",
)
@click.pass_context
def identitymgr_command(ctx: click.Context) -> None:
    ctx.obj = {}


@identitymgr_command.group("user", cls=HelpSuffixGroup, help="Manage local users.")
def user_group() -> None:
    pass


@user_group.command("create", help="Create a local user.")
@click.argument("email")
@_password_source_option(default=PASSWORD_SOURCE_PROMPT)
@click.option("--admin", is_flag=True)
@click.option("--superuser", is_flag=True)
@click.option("--unverified", is_flag=True)
@click.option("--display-name")
@click.option("--preferred-name")
@click.option("--timezone", "preferred_timezone")
@click.option("--expires-at", callback=_timestamp_callback)
@click.option("--group", "groups", multiple=True)
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
    groups: tuple[str, ...],
) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="create",
            email=email,
            password=password,
            admin=admin,
            superuser=superuser,
            unverified=unverified,
            display_name=display_name,
            preferred_name=preferred_name,
            preferred_timezone=preferred_timezone,
            expires_at=expires_at,
            add_groups=groups,
        ),
    )


@user_group.command("update", help="Update a local user.")
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
@click.option("--add-group", "add_groups", multiple=True)
@click.option("--rm-group", "remove_groups", multiple=True)
@click.option("--set-group", "set_groups", multiple=True)
@click.option("--group", "invalid_groups", multiple=True)
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
    add_groups: tuple[str, ...],
    remove_groups: tuple[str, ...],
    set_groups: tuple[str, ...],
    invalid_groups: tuple[str, ...],
) -> None:
    if invalid_groups:
        raise click.UsageError(
            "Do not use --group with update; use --set-group for replacement "
            "or --add-group/--rm-group for incremental changes."
        )
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
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="update",
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
            add_groups=add_groups,
            remove_groups=remove_groups,
            set_groups=set_groups,
        ),
    )


@user_group.command("delete", help="Delete a local user.")
@click.argument("target")
@click.option("--force", is_flag=True)
@click.pass_context
def delete_command(ctx: click.Context, target: str, force: bool) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="delete",
            target=target,
            force=force,
        ),
    )


@user_group.command("deactivate", help="Deactivate a local user.")
@click.argument("target")
@click.option("--force", is_flag=True)
@click.pass_context
def deactivate_command(ctx: click.Context, target: str, force: bool) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="deactivate",
            target=target,
            force=force,
        ),
    )


@user_group.command("list", help="List local users.")
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
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="list",
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


@user_group.command("password", help="Change a local user's password.")
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
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="password",
            target=target,
            password=password,
            no_revoke=no_revoke,
        ),
    )


@identitymgr_command.group(
    "scope",
    cls=HelpSuffixGroup,
    help="Manage authorisation scopes.",
)
def scope_group() -> None:
    pass


@scope_group.command("create", help="Create an authorisation scope.")
@click.argument("scope")
@click.option("--description")
@click.pass_context
def scope_create_command(
    ctx: click.Context,
    scope: str,
    description: str | None,
) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="scope-create",
            scope=scope,
            description=description,
        ),
    )


@scope_group.command("update", help="Update an authorisation scope.")
@click.argument("scope")
@click.option("--description")
@click.pass_context
def scope_update_command(
    ctx: click.Context,
    scope: str,
    description: str | None,
) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="scope-update",
            scope=scope,
            description=description,
        ),
    )


@scope_group.command("delete", help="Delete an unused authorisation scope.")
@click.argument("scope")
@click.pass_context
def scope_delete_command(ctx: click.Context, scope: str) -> None:
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="scope-delete",
            scope=scope,
        ),
    )


@scope_group.command("list", help="List authorisation scopes.")
@click.option("--json", "json_output", is_flag=True)
@click.option("--csv", "csv_output", is_flag=True)
@click.pass_context
def scope_list_command(
    ctx: click.Context,
    json_output: bool,
    csv_output: bool,
) -> None:
    _ensure_mutually_exclusive((json_output, "--json"), (csv_output, "--csv"))
    _run_identitymgr(
        ctx,
        IdentitymgrArgs(
            command="scope-list",
            json_output=json_output,
            csv_output=csv_output,
        ),
    )


@identitymgr_command.command(
    "group",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    help="Manage authorisation groups.",
)
@click.argument("tokens", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def group_command(ctx: click.Context, tokens: tuple[str, ...]) -> None:
    if tokens == ("help",):
        click.echo(ctx.get_help(), color=ctx.color)
        return
    if tokens and tokens[0] == "help":
        click.echo(_group_operation_help(tokens[1:]), color=ctx.color)
        return
    _run_identitymgr(ctx, _group_args(ctx, tokens))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = identitymgr_command.main(
            args=None if argv is None else list(argv),
            prog_name=PROGRAM_NAME,
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


async def _main_async(args: IdentitymgrArgs) -> int:
    settings = _load_auth_settings_for_command()
    database = create_database(settings.database_url)
    try:
        async with session_scope(database.session_factory) as session:
            await _verify_identity_schema(session)
            match args.command:
                case "create":
                    replacement_group_ids: list[UUID] = []
                    if args.add_groups:
                        group_result = await _resolve_group_targets_for_set(
                            session,
                            args.add_groups,
                        )
                        if group_result.is_failure():
                            return _print_failure(
                                group_result.error_type,
                                group_result.message,
                            )
                        replacement_group_ids = cast(
                            list[UUID],
                            (group_result.value or {}).get("group_ids", []),
                        )

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

                    user, target_error = await resolve_user_target(session, args.email)
                    if target_error is not None:
                        return _print_failure(
                            target_error,
                            target_error_message(target_error),
                        )
                    if user is None:
                        return _print_failure(
                            ERROR_NOT_FOUND,
                            "No matching user was found.",
                        )

                    for group_id in dict.fromkeys(replacement_group_ids):
                        session.add(GroupUser(group_id=group_id, user_id=user.id))
                    if replacement_group_ids:
                        await session.commit()

                    value = result.value or {}
                    print(f"created user: {value.get('email', args.email)}")
                    return 0
                case "update":
                    password = (
                        _read_password(args.password)
                        if args.password is not None
                        else None
                    )
                    result: Result[dict[str, Any]] = Result.ok({})
                    if _user_metadata_update_requested(args, password):
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
                    else:
                        if not (
                            args.add_groups or args.remove_groups or args.set_groups
                        ):
                            return _print_failure(
                                ERROR_NO_CHANGES,
                                "No user changes were requested.",
                            )
                        result = await _resolve_target_record(session, args.target)
                        if result.is_failure():
                            return _print_failure(result.error_type, result.message)

                    membership_result = await _update_user_groups_from_args(
                        session,
                        args,
                    )
                    if membership_result.is_failure():
                        return _print_failure(
                            membership_result.error_type,
                            membership_result.message,
                        )

                    value = {
                        **(result.value or {}),
                        **(membership_result.value or {}),
                    }
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
                case "scope-create":
                    result = await create_scope_for_management(
                        session,
                        scope=args.scope,
                        description=args.description,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"created scope: {value.get('scope', args.scope)}")
                    return 0
                case "scope-update":
                    result = await update_scope_for_management(
                        session,
                        scope=args.scope,
                        description=args.description,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"updated scope: {value.get('scope', args.scope)}")
                    return 0
                case "scope-delete":
                    result = await delete_scope_for_management(
                        session,
                        scope=args.scope,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deleted scope: {value.get('scope', args.scope)}")
                    return 0
                case "scope-list":
                    result = await list_scopes_for_management(session)
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    _print_records(
                        (result.value or {}).get("scopes", []),
                        field_names=("scope", "description"),
                        json_output=args.json_output,
                        csv_output=args.csv_output,
                    )
                    return 0
                case "group-create":
                    result = await create_group_for_management(
                        session,
                        abbrev=args.group_target,
                        description=args.description or "",
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    for scope in args.add_scopes:
                        result = await add_scope_to_group_for_management(
                            session,
                            group_target=args.group_target,
                            scope=scope,
                        )
                        if result.is_failure():
                            return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"created group: {value.get('abbrev', args.group_target)}")
                    return 0
                case "group-list":
                    result = await list_groups_for_management(session)
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    _print_records(
                        (result.value or {}).get("groups", []),
                        field_names=(
                            "id",
                            "abbrev",
                            "description",
                            "scopes",
                            "users",
                            "child_groups",
                            "parent_groups",
                        ),
                        json_output=args.json_output,
                        csv_output=args.csv_output,
                    )
                    return 0
                case "group-show":
                    result = await get_group_for_management(
                        session,
                        target=args.group_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    _print_single_record(
                        result.value or {},
                        json_output=args.json_output,
                    )
                    return 0
                case "group-update":
                    result = await _update_group_from_args(session, args)
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"updated group: {value.get('abbrev', args.group_target)}")
                    return 0
                case "group-delete":
                    result = await delete_group_for_management(
                        session,
                        target=args.group_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deleted group: {value.get('abbrev', args.group_target)}")
                    return 0
                case "group-add-user":
                    result = await add_user_to_group_for_management(
                        session,
                        group_target=args.group_target,
                        user_target=args.user_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    print(f"added user to group: {args.group_target}")
                    return 0
                case "group-remove-user":
                    result = await remove_user_from_group_for_management(
                        session,
                        group_target=args.group_target,
                        user_target=args.user_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    print(f"removed user from group: {args.group_target}")
                    return 0
                case "group-add-group":
                    result = await add_child_group_to_group_for_management(
                        session,
                        parent_target=args.group_target,
                        child_target=args.child_group_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    print(f"added child group: {args.group_target}")
                    return 0
                case "group-remove-group":
                    result = await remove_child_group_from_group_for_management(
                        session,
                        parent_target=args.group_target,
                        child_target=args.child_group_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    print(f"removed child group: {args.group_target}")
                    return 0
                case "group-effective-scopes":
                    result = await effective_scopes_for_user_for_management(
                        session,
                        user_target=args.user_target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    _print_single_record(
                        result.value or {},
                        json_output=args.json_output,
                    )
                    return 0
                case _:
                    print(f"{args.command}: not implemented", file=sys.stderr)
                    return 1
    finally:
        await close_database(database)


def _load_auth_settings_for_command() -> AuthSettings:
    try:
        project_root = runtime_project_root()
        app_config = load_app_config(project_root=project_root)
    except ProjectToolConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc
    except CompositionError as exc:
        raise ConfigurationError(
            f"{exc}. Run from a Wevra host application project or set APP_CONFIG."
        ) from exc

    return load_auth_settings(app_config=app_config)


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


async def _update_group_from_args(
    session: AsyncSession,
    args: IdentitymgrArgs,
) -> Result[dict[str, Any]]:
    result: Result[dict[str, Any]] | None = None
    if args.description is not None:
        result = await update_group_for_management(
            session,
            target=args.group_target,
            description=args.description,
        )
        if result.is_failure():
            return result

    for scope in args.add_scopes:
        result = await add_scope_to_group_for_management(
            session,
            group_target=args.group_target,
            scope=scope,
        )
        if result.is_failure():
            return result

    for scope in args.remove_scopes:
        result = await remove_scope_from_group_for_management(
            session,
            group_target=args.group_target,
            scope=scope,
        )
        if result.is_failure():
            return result

    if result is None:
        return Result.failure(ERROR_NO_CHANGES, "No group changes were requested.")

    return result


def _user_metadata_update_requested(
    args: IdentitymgrArgs,
    password: str | None,
) -> bool:
    return any(
        value is not None
        for value in (
            args.is_admin,
            args.is_superuser,
            args.is_verified,
            password,
            args.display_name,
            args.preferred_name,
            args.preferred_timezone,
            args.expires_at,
        )
    ) or any(
        (
            args.clear_display_name,
            args.clear_preferred_name,
            args.clear_preferred_timezone,
            args.no_expires_at,
        )
    )


async def _update_user_groups_from_args(
    session: AsyncSession,
    args: IdentitymgrArgs,
) -> Result[dict[str, Any]]:
    if not (args.add_groups or args.remove_groups or args.set_groups):
        return Result.ok({})

    user, target_error = await resolve_user_target(session, args.target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if args.set_groups:
        replacement_group_result = await _resolve_group_targets_for_set(
            session,
            args.set_groups,
        )
        if replacement_group_result.is_failure():
            return replacement_group_result
        replacement_group_ids = cast(
            list[UUID],
            (replacement_group_result.value or {}).get("group_ids", []),
        )

        await session.execute(delete(GroupUser).where(GroupUser.user_id == user.id))
        for group_id in dict.fromkeys(replacement_group_ids):
            session.add(GroupUser(group_id=group_id, user_id=user.id))
        await session.commit()

    for group_target in args.add_groups:
        result = await add_user_to_group_for_management(
            session,
            group_target=group_target,
            user_target=args.target,
        )
        if result.is_failure():
            return result

    for group_target in args.remove_groups:
        result = await remove_user_from_group_for_management(
            session,
            group_target=group_target,
            user_target=args.target,
        )
        if result.is_failure():
            return result

    return Result.ok(user_record(user))


async def _resolve_group_targets_for_set(
    session: AsyncSession,
    group_targets: tuple[str, ...],
) -> Result[dict[str, Any]]:
    unique_targets = tuple(dict.fromkeys(group_targets))
    groups_by_abbrev = {
        group.abbrev: group
        for group in (
            await session.execute(select(Group).where(Group.abbrev.in_(unique_targets)))
        )
        .scalars()
        .all()
    }
    parsed_ids: dict[str, UUID] = {}
    invalid_targets: set[str] = set()
    for target in unique_targets:
        if target in groups_by_abbrev:
            continue
        try:
            parsed_ids[target] = UUID(target)
        except ValueError:
            invalid_targets.add(target)

    groups_by_id: dict[UUID, Group] = {}
    if parsed_ids:
        groups_by_id = {
            group.id: group
            for group in (
                await session.execute(
                    select(Group).where(Group.id.in_(parsed_ids.values()))
                )
            )
            .scalars()
            .all()
        }

    group_ids = []
    for target in group_targets:
        if target in groups_by_abbrev:
            group_ids.append(groups_by_abbrev[target].id)
            continue

        if target in invalid_targets:
            return Result.failure(
                ERROR_INVALID_GROUP_ID,
                group_target_error_message(ERROR_INVALID_GROUP_ID),
            )

        group_id = parsed_ids[target]
        group = groups_by_id.get(group_id)
        if group is None:
            return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")
        group_ids.append(group.id)

    return Result.ok({"group_ids": group_ids})


def _print_failure(error_type: str | None, message: str | None) -> int:
    fallback_messages = {
        ERROR_ALREADY_EXISTS: "User already exists.",
        ERROR_CYCLIC_GROUP_MEMBERSHIP: "Nested group membership would create a cycle.",
        ERROR_FINAL_SUPERUSER: "Cannot remove the final superuser flag.",
        ERROR_GROUP_HAS_MEMBERSHIPS: "Group still has memberships.",
        ERROR_INVALID_EMAIL: "Email address is invalid.",
        ERROR_INVALID_PASSWORD: "Password is invalid.",
        ERROR_INVALID_TIMEZONE: "Preferred timezone is invalid.",
        ERROR_INVALID_USER_ID: "User target must be an email address or valid user ID.",
        ERROR_NO_CHANGES: "No user changes were requested.",
        ERROR_NOT_FOUND: "No matching user was found.",
        ERROR_SCOPE_IN_USE: "Scope is assigned to one or more groups.",
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


def _print_records(
    records: list[dict[str, Any]],
    *,
    field_names: tuple[str, ...],
    json_output: bool,
    csv_output: bool,
) -> None:
    cleaned_records = [
        _record_without_nulls_for_fields(record, field_names) for record in records
    ]
    if json_output:
        print(json.dumps(cleaned_records))
        return

    formatted_records = [
        {
            field_name: _format_record_value(value)
            for field_name, value in record.items()
        }
        for record in cleaned_records
    ]
    if csv_output:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(field_names))
        writer.writeheader()
        writer.writerows(formatted_records)
        return

    for record in formatted_records:
        print(
            " ".join(
                f"{field_name}={record.get(field_name)}"
                for field_name in field_names
                if field_name in record
            )
        )


def _print_single_record(record: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(_record_without_nulls_for_fields(record, tuple(record))))
        return

    for field_name, value in _record_without_nulls_for_fields(
        record,
        tuple(record),
    ).items():
        print(f"{field_name}={_format_record_value(value)}")


def _record_without_nulls_for_fields(
    record: dict[str, Any],
    field_names: tuple[str, ...],
) -> dict[str, Any]:
    return {
        field_name: record[field_name]
        for field_name in field_names
        if field_name in record and record[field_name] is not None
    }


def _format_record_value(value: Any) -> str:
    if isinstance(value, str | int | float | bool) or value is None:
        return str(value)
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


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
