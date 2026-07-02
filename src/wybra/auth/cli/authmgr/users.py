from __future__ import annotations

import click

from .args import REVOKE_ALL_PASSKEYS, AuthmgrArgs
from .clicking import (
    HelpSuffixGroup,
    _ensure_mutually_exclusive,
    _optional_boolean,
    _password_source_option,
    _timestamp_callback,
)
from .passwords import PASSWORD_SOURCE_PROMPT, PasswordSource
from .runtime import _run_authmgr


def register_user_commands(root_command: click.Group) -> None:
    @root_command.group("user", cls=HelpSuffixGroup, help="Manage local users.")
    def user_group() -> None:
        pass

    @user_group.command("create", help="Create a local user.")
    @click.argument("email")
    @_password_source_option(default=PASSWORD_SOURCE_PROMPT)
    @click.option("--admin", is_flag=True)
    @click.option("--superuser", is_flag=True)
    @click.option("--unverified", is_flag=True)
    @click.option("--timezone", "preferred_timezone")
    @click.option("--expires-at", callback=_timestamp_callback)
    @click.option("--group", "groups", multiple=True)
    @click.option(
        "--totp",
        is_flag=True,
        help=(
            "Provision TOTP secret material and recovery codes for the target "
            "user. Output contains secrets printed once for secure storage; "
            "do not log or expose it."
        ),
    )
    @click.option("--json", "json_output", is_flag=True)
    @click.option(
        "--include-secrets",
        is_flag=True,
        help=(
            "Include generated TOTP secrets and recovery codes in JSON output. "
            "Sensitive; use only for secure handoff."
        ),
    )
    @click.pass_context
    def create_command(
        ctx: click.Context,
        email: str,
        password: PasswordSource,
        admin: bool,
        superuser: bool,
        unverified: bool,
        preferred_timezone: str | None,
        expires_at: float | None,
        groups: tuple[str, ...],
        totp: bool,
        json_output: bool,
        include_secrets: bool,
    ) -> None:
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="create",
                email=email,
                password=password,
                admin=admin,
                superuser=superuser,
                unverified=unverified,
                preferred_timezone=preferred_timezone,
                expires_at=expires_at,
                add_groups=groups,
                totp=totp,
                json_output=json_output,
                include_secrets=include_secrets,
            ),
        )

    @user_group.command("update", help="Update a local user.")
    @click.argument("target")
    @click.option("--admin", "admin", is_flag=True, help="Grant admin privileges.")
    @click.option(
        "--no-admin",
        "no_admin",
        is_flag=True,
        help="Remove admin privileges.",
    )
    @click.option(
        "--superuser",
        "superuser",
        is_flag=True,
        help="Grant superuser privileges.",
    )
    @click.option(
        "--no-superuser",
        "no_superuser",
        is_flag=True,
        help="Remove superuser privileges when this is not the final superuser.",
    )
    @click.option("--verify", "verify", is_flag=True, help="Mark the user as verified.")
    @click.option(
        "--no-verify",
        "no_verify",
        is_flag=True,
        help="Mark the user as unverified.",
    )
    @_password_source_option(default=None)
    @click.option(
        "--no-revoke",
        is_flag=True,
        help="Keep existing sessions when changing the user's password.",
    )
    @click.option(
        "--timezone",
        "preferred_timezone",
        help="Set the user's preferred IANA timezone.",
    )
    @click.option(
        "--no-timezone",
        "clear_preferred_timezone",
        is_flag=True,
        help="Clear the user's preferred timezone.",
    )
    @click.option(
        "--expires-at",
        callback=_timestamp_callback,
        help="Set the account expiry timestamp.",
    )
    @click.option(
        "--no-expires-at",
        is_flag=True,
        help="Clear the account expiry timestamp.",
    )
    @click.option(
        "--add-group", "add_groups", multiple=True, help="Add group membership."
    )
    @click.option(
        "--rm-group",
        "remove_groups",
        multiple=True,
        help="Remove group membership.",
    )
    @click.option(
        "--set-group",
        "set_groups",
        multiple=True,
        help="Replace all group memberships; may be provided more than once.",
    )
    @click.option(
        "--group",
        "invalid_groups",
        multiple=True,
        help="Removed update shortcut. Use --set-group, --add-group, or --rm-group.",
    )
    @click.option(
        "--totp",
        is_flag=True,
        help=(
            "Replace the target user's active TOTP credential and print secret "
            "material plus recovery codes once for secure storage; do not log "
            "or expose it."
        ),
    )
    @click.option("--no-totp", is_flag=True, help="Disable the active TOTP credential.")
    @click.option(
        "--rcodes",
        is_flag=True,
        help=(
            "Rotate recovery codes for the active TOTP credential and print "
            "the new secret codes once for secure storage; do not log or expose "
            "them."
        ),
    )
    @click.option(
        "--revoke-passkey",
        is_flag=False,
        flag_value=REVOKE_ALL_PASSKEYS,
        default=None,
        metavar="[CREDENTIAL]",
        help=(
            "Revoke active passkeys for the target user. Omit CREDENTIAL to "
            "revoke all active passkeys, or provide a passkey id or credential "
            "id to revoke one passkey."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Print JSON output.")
    @click.option(
        "--include-secrets",
        is_flag=True,
        help=(
            "Include generated TOTP secrets and recovery codes in JSON output. "
            "Sensitive; use only for secure handoff."
        ),
    )
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
        preferred_timezone: str | None,
        clear_preferred_timezone: bool,
        expires_at: float | None,
        no_expires_at: bool,
        add_groups: tuple[str, ...],
        remove_groups: tuple[str, ...],
        set_groups: tuple[str, ...],
        invalid_groups: tuple[str, ...],
        totp: bool,
        no_totp: bool,
        rcodes: bool,
        revoke_passkey: str | None,
        json_output: bool,
        include_secrets: bool,
    ) -> None:
        if invalid_groups:
            raise click.UsageError(
                "Do not use --group with update; use --set-group for replacement "
                "or --add-group/--rm-group for incremental changes."
            )
        if set_groups and (add_groups or remove_groups):
            raise click.UsageError(
                "--set-group cannot be used with --add-group or --rm-group."
            )
        _ensure_mutually_exclusive(
            (preferred_timezone, "--timezone"),
            (clear_preferred_timezone, "--no-timezone"),
        )
        _ensure_mutually_exclusive(
            (expires_at, "--expires-at"), (no_expires_at, "--no-expires-at")
        )
        _ensure_mutually_exclusive((totp, "--totp"), (no_totp, "--no-totp"))
        _ensure_mutually_exclusive((totp, "--totp"), (rcodes, "--rcodes"))
        _ensure_mutually_exclusive((no_totp, "--no-totp"), (rcodes, "--rcodes"))
        _ensure_mutually_exclusive(
            (totp, "--totp"),
            (revoke_passkey, "--revoke-passkey"),
        )
        _ensure_mutually_exclusive(
            (no_totp, "--no-totp"),
            (revoke_passkey, "--revoke-passkey"),
        )
        _ensure_mutually_exclusive(
            (rcodes, "--rcodes"),
            (revoke_passkey, "--revoke-passkey"),
        )
        _run_authmgr(
            ctx,
            AuthmgrArgs(
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
                preferred_timezone=preferred_timezone,
                clear_preferred_timezone=clear_preferred_timezone,
                expires_at=expires_at,
                no_expires_at=no_expires_at,
                add_groups=add_groups,
                remove_groups=remove_groups,
                set_groups=set_groups,
                totp=totp,
                no_totp=no_totp,
                rcodes=rcodes,
                revoke_passkey=revoke_passkey,
                json_output=json_output,
                include_secrets=include_secrets,
            ),
        )

    @user_group.command("delete", help="Delete a local user.")
    @click.argument("target")
    @click.option("--force", is_flag=True)
    @click.pass_context
    def delete_command(ctx: click.Context, target: str, force: bool) -> None:
        _run_authmgr(
            ctx,
            AuthmgrArgs(
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
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="deactivate",
                target=target,
                force=force,
            ),
        )

    @user_group.command("list", help="List local users.")
    @click.option("--json", "json_output", is_flag=True)
    @click.option("--csv", "csv_output", is_flag=True)
    @click.option(
        "--passkeys",
        "include_passkeys",
        is_flag=True,
        help="Include active passkey records for each listed user.",
    )
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
        include_passkeys: bool,
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
        _ensure_mutually_exclusive(
            (include_passkeys, "--passkeys"),
            (csv_output, "--csv"),
        )
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="list",
                json_output=json_output,
                csv_output=csv_output,
                include_passkeys=include_passkeys,
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
        _run_authmgr(
            ctx,
            AuthmgrArgs(
                command="password",
                target=target,
                password=password,
                no_revoke=no_revoke,
            ),
        )
