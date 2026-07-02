from __future__ import annotations

import click

from .args import AuthmgrArgs
from .clicking import _ensure_mutually_exclusive, _parse_cli_tokens
from .runtime import _run_authmgr

_OPTION_TERMINATOR_SENTINEL = "\0wybra-authmgr-option-terminator"
_HELP_OPTIONS = {"-h", "--help"}


class _GroupCommand(click.Command):
    def get_help(self, ctx: click.Context) -> str:
        return f"{super().get_help(ctx).rstrip()}\n\n{_GROUP_OPERATIONS_HELP}\n"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        help_args = _group_help_args(args)
        if help_args is not None:
            click.echo(_group_operation_help(help_args), color=ctx.color)
            ctx.exit()
        if "--" in args:
            normalised_args = list(args)
            normalised_args[normalised_args.index("--")] = _OPTION_TERMINATOR_SENTINEL
            args = normalised_args
        return super().parse_args(ctx, args)


def _group_help_args(args: list[str]) -> tuple[str, ...] | None:
    if not any(arg in _HELP_OPTIONS for arg in args):
        return None

    try:
        terminator_index = args.index("--")
    except ValueError:
        help_candidate_args = args
    else:
        help_candidate_args = args[:terminator_index]

    if not any(arg in _HELP_OPTIONS for arg in help_candidate_args):
        return None

    help_args = tuple(arg for arg in help_candidate_args if arg not in _HELP_OPTIONS)
    return help_args or None


def _restore_option_terminator(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        "--" if token == _OPTION_TERMINATOR_SENTINEL else token for token in tokens
    )


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
                raise click.UsageError("Usage: wybra-authmgr group create <abbrev>.")
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
                    "Usage: wybra-authmgr group list [--json|--csv]."
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
                    "Usage: wybra-authmgr group effective-scopes <user-target>."
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
                    "Usage: wybra-authmgr group <group> show [--json]."
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
                    "Usage: wybra-authmgr group <group> update "
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
                    "Usage: wybra-authmgr group <group> delete [--force]."
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
                    f"Usage: wybra-authmgr group <group> {operation} <user>."
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
                    f"Usage: wybra-authmgr group <group> {operation} <group>."
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
        "Usage: wybra-authmgr group create <abbrev> "
        "[--description <text>] [--scope <scope>]."
    ),
    "list": "Usage: wybra-authmgr group list [--json|--csv].",
    "effective-scopes": (
        "Usage: wybra-authmgr group effective-scopes <user-target> [--json]."
    ),
}

_GROUP_TARGET_OPERATION_HELP = {
    "show": "Usage: wybra-authmgr group <group> show [--json].",
    "update": (
        "Usage: wybra-authmgr group <group> update "
        "[--description <text>] [--scope <scope>] [--rm-scope <scope>]."
    ),
    "delete": "Usage: wybra-authmgr group <group> delete [--force].",
    "add-user": "Usage: wybra-authmgr group <group> add-user <user>.",
    "remove-user": "Usage: wybra-authmgr group <group> remove-user <user>.",
    "add-group": "Usage: wybra-authmgr group <group> add-group <group>.",
    "remove-group": "Usage: wybra-authmgr group <group> remove-group <group>.",
}

_GROUP_OPERATIONS_HELP = """Operations:

  wybra-authmgr group create <abbrev> [--description <text>] [--scope <scope>]
  wybra-authmgr group list [--json|--csv]
  wybra-authmgr group effective-scopes <user-target> [--json]
  wybra-authmgr group <group> show [--json]
  wybra-authmgr group <group> update [options]
  wybra-authmgr group <group> delete [--force]
  wybra-authmgr group <group> add-user <user>
  wybra-authmgr group <group> remove-user <user>
  wybra-authmgr group <group> add-group <group>
  wybra-authmgr group <group> remove-group <group>

Use 'wybra-authmgr group <operation> --help' or
'wybra-authmgr group <group> <operation> --help' for operation usage."""

_GROUP_COMMAND_HELP = "Manage authorisation groups."


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
            "Try 'wybra-authmgr group --help'."
        )
    return help_text


def register_group_commands(root_command: click.Group) -> None:
    @root_command.command(
        "group",
        cls=_GroupCommand,
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "max_content_width": 120,
        },
        help=_GROUP_COMMAND_HELP,
    )
    @click.argument(
        "tokens",
        metavar="[OPERATION]...",
        nargs=-1,
        type=click.UNPROCESSED,
    )
    @click.pass_context
    def group_command(ctx: click.Context, tokens: tuple[str, ...]) -> None:
        tokens = _restore_option_terminator(tokens)
        if tokens == ("help",):
            click.echo(ctx.get_help(), color=ctx.color)
            return
        if tokens and tokens[0] == "help":
            click.echo(_group_operation_help(tokens[1:]), color=ctx.color)
            return
        _run_authmgr(ctx, _group_args(ctx, tokens))
