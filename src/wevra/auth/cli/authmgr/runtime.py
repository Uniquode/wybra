from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

import click
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.admin.management import (
    ERROR_INVALID_GROUP_ID,
    ERROR_NO_CHANGES,
    ERROR_NOT_FOUND,
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
from wevra.auth.models import Group, GroupUser
from wevra.auth.result import Result
from wevra.auth.settings import AuthSettings, load_auth_settings
from wevra.core.composition import CompositionError, load_app_config
from wevra.db.persistence import close_database, create_database, session_scope
from wevra.tools.project import ProjectToolConfigurationError, runtime_project_root

from .args import AuthmgrArgs
from .output import (
    _print_failure,
    _print_records,
    _print_single_record,
    _print_user_records,
)
from .passwords import PasswordSourceError, _read_password, _read_required_password
from .schema import _verify_identity_schema


def _run_authmgr(ctx: click.Context, args: AuthmgrArgs) -> None:
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


async def _main_async(args: AuthmgrArgs) -> int:
    settings = _load_auth_settings_for_command()
    database = create_database(settings.database_url)
    try:
        async with session_scope(database.session_factory) as session:
            await _verify_identity_schema(session)
            return await _run_command(session, settings, args)
    finally:
        await close_database(database)


async def _run_command(
    session: AsyncSession,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        print(f"{args.command}: not implemented", file=sys.stderr)
        return 1
    return await handler(session, settings, args)


async def _handle_user_create(
    session: AsyncSession,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    replacement_group_ids: list[UUID] = []
    if args.add_groups:
        group_result = await _resolve_group_targets_for_set(
            session,
            args.add_groups,
        )
        if group_result.is_failure():
            return _print_failure(group_result.error_type, group_result.message)
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
        return _print_failure(target_error, target_error_message(target_error))
    if user is None:
        return _print_failure(ERROR_NOT_FOUND, "No matching user was found.")

    for group_id in dict.fromkeys(replacement_group_ids):
        session.add(GroupUser(group_id=group_id, user_id=user.id))
    if replacement_group_ids:
        await session.commit()

    value = result.value or {}
    print(f"created user: {value.get('email', args.email)}")
    return 0


async def _handle_user_update(
    session: AsyncSession,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    password = _read_password(args.password) if args.password is not None else None
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
        if not (args.add_groups or args.remove_groups or args.set_groups):
            return _print_failure(ERROR_NO_CHANGES, "No user changes were requested.")
        result = await _resolve_target_record(session, args.target)
        if result.is_failure():
            return _print_failure(result.error_type, result.message)

    membership_result = await _update_user_groups_from_args(session, args)
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


async def _handle_user_delete(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    if not args.force:
        target_result = await _resolve_target_record(session, args.target)
        if target_result.is_failure():
            return _print_failure(target_result.error_type, target_result.message)
        target_record = target_result.value or {}
        if not _confirm_destructive("delete", target_record):
            return 1

    result = await delete_local_user_for_management(session, target=args.target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted user: {value.get('email', args.target)}")
    return 0


async def _handle_user_deactivate(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    if not args.force:
        target_result = await _resolve_target_record(session, args.target)
        if target_result.is_failure():
            return _print_failure(target_result.error_type, target_result.message)
        target_record = target_result.value or {}
        if not _confirm_destructive("deactivate", target_record):
            return 1

    result = await deactivate_local_user_for_management(session, target=args.target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deactivated user: {value.get('email', args.target)}")
    return 0


async def _handle_user_list(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_user_password(
    session: AsyncSession,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_scope_create(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_scope_update(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_scope_delete(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await delete_scope_for_management(session, scope=args.scope)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted scope: {value.get('scope', args.scope)}")
    return 0


async def _handle_scope_list(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_group_create(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_group_list(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
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


async def _handle_group_show(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await get_group_for_management(session, target=args.group_target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    _print_single_record(result.value or {}, json_output=args.json_output)
    return 0


async def _handle_group_update(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await _update_group_from_args(session, args)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"updated group: {value.get('abbrev', args.group_target)}")
    return 0


async def _handle_group_delete(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await delete_group_for_management(session, target=args.group_target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted group: {value.get('abbrev', args.group_target)}")
    return 0


async def _handle_group_add_user(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await add_user_to_group_for_management(
        session,
        group_target=args.group_target,
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"added user to group: {args.group_target}")
    return 0


async def _handle_group_remove_user(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await remove_user_from_group_for_management(
        session,
        group_target=args.group_target,
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"removed user from group: {args.group_target}")
    return 0


async def _handle_group_add_group(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await add_child_group_to_group_for_management(
        session,
        parent_target=args.group_target,
        child_target=args.child_group_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"added child group: {args.group_target}")
    return 0


async def _handle_group_remove_group(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await remove_child_group_from_group_for_management(
        session,
        parent_target=args.group_target,
        child_target=args.child_group_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"removed child group: {args.group_target}")
    return 0


async def _handle_group_effective_scopes(
    session: AsyncSession,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await effective_scopes_for_user_for_management(
        session,
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    _print_single_record(result.value or {}, json_output=args.json_output)
    return 0


CommandHandler = Callable[[AsyncSession, AuthSettings, AuthmgrArgs], Awaitable[int]]

_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "create": _handle_user_create,
    "update": _handle_user_update,
    "delete": _handle_user_delete,
    "deactivate": _handle_user_deactivate,
    "list": _handle_user_list,
    "password": _handle_user_password,
    "scope-create": _handle_scope_create,
    "scope-update": _handle_scope_update,
    "scope-delete": _handle_scope_delete,
    "scope-list": _handle_scope_list,
    "group-create": _handle_group_create,
    "group-list": _handle_group_list,
    "group-show": _handle_group_show,
    "group-update": _handle_group_update,
    "group-delete": _handle_group_delete,
    "group-add-user": _handle_group_add_user,
    "group-remove-user": _handle_group_remove_user,
    "group-add-group": _handle_group_add_group,
    "group-remove-group": _handle_group_remove_group,
    "group-effective-scopes": _handle_group_effective_scopes,
}


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


async def _resolve_target_record(
    session: AsyncSession,
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
    args: AuthmgrArgs,
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
    args: AuthmgrArgs,
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
    args: AuthmgrArgs,
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
