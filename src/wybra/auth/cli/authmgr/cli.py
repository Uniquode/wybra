from __future__ import annotations

import sys
from collections.abc import Sequence

import click

from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
)

from .args import PROGRAM_NAME
from .clicking import CONTEXT_SETTINGS, HelpSuffixGroup
from .groups import register_group_commands
from .scopes import register_scope_commands
from .timestamps import TIMESTAMP_HELP
from .users import register_user_commands


@click.group(
    name=PROGRAM_NAME,
    cls=HelpSuffixGroup,
    context_settings=CONTEXT_SETTINGS,
    epilog=TIMESTAMP_HELP,
    help="Manage local identity resources through configured services.",
)
@click.option(CONFIG_SOURCE_OPTION, CONFIG_SOURCE_CONTEXT_KEY, help=CONFIG_SOURCE_HELP)
@click.pass_context
def authmgr_command(ctx: click.Context, config_source: str | None) -> None:
    ctx.obj = {CONFIG_SOURCE_CONTEXT_KEY: config_source}


register_user_commands(authmgr_command)
register_scope_commands(authmgr_command)
register_group_commands(authmgr_command)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = authmgr_command.main(
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
