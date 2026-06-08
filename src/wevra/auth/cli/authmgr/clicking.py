from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import click

from .passwords import (
    PASSWORD_SOURCE_PROMPT,
    PASSWORD_SOURCE_STDIN,
    PASSWORD_SOURCE_STDIN_ALIAS,
    PasswordSource,
    PasswordSourceInput,
)
from .timestamps import parse_timestamp_filter

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
    value: PasswordSourceInput | str | None,
) -> PasswordSource | None:
    """Normalise supported password-source inputs for runtime handling.

    ``None`` is preserved for optional password-update flows where omitting the
    option means "leave the password unchanged". The ``stdin`` CLI alias is
    converted to ``-`` before any value reaches password reading.
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
