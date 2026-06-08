from __future__ import annotations

import click

from .args import AuthmgrArgs
from .clicking import _ensure_mutually_exclusive, _parse_cli_tokens
from .runtime import _run_authmgr


def _group_args(ctx: click.Context, tokens: tuple[str, ...]) -> AuthmgrArgs:
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
                command="group-effective-scopes",
                user_target=parsed.positionals[0],
                json_output=parsed.has_flag("--json"),
            )
        case _:
            return _target_group_args(ctx, tokens)


def _target_group_args(ctx: click.Context, tokens: tuple[str, ...]) -> AuthmgrArgs:
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
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
            return AuthmgrArgs(
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


def register_group_commands(root_command: click.Group) -> None:
    @root_command.command(
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
        _run_authmgr(ctx, _group_args(ctx, tokens))
