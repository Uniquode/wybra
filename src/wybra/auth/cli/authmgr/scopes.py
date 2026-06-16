from __future__ import annotations

import click

from .args import AuthmgrArgs
from .clicking import HelpSuffixGroup, _ensure_mutually_exclusive
from .runtime import _run_authmgr


def register_scope_commands(root_command: click.Group) -> None:
    @root_command.group(
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
        _run_authmgr(
            ctx,
            AuthmgrArgs(
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
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="scope-update",
                scope=scope,
                description=description,
            ),
        )

    @scope_group.command("delete", help="Delete an unused authorisation scope.")
    @click.argument("scope")
    @click.pass_context
    def scope_delete_command(ctx: click.Context, scope: str) -> None:
        _run_authmgr(
            ctx,
            AuthmgrArgs(
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
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="scope-list",
                json_output=json_output,
                csv_output=csv_output,
            ),
        )
