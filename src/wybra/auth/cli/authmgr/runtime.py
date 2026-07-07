from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import click

from wybra.auth.admin.management import (
    ERROR_NO_CHANGES,
)
from wybra.auth.persistence import auth_persistence_session
from wybra.auth.persistence.contracts import AuthManagementStore, AuthPersistenceScope
from wybra.auth.result import Result
from wybra.auth.settings import AuthSettings
from wybra.config.service import ConfigService
from wybra.config.sources import AppConfigSource
from wybra.core.composition import AppConfig, CompositionError, load_app_config
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.exceptions import ConfigurationError
from wybra.core.logging import LoggingConfigurationError
from wybra.db.persistence import close_database, create_database
from wybra.secrets.capabilities import DefaultSecretsCapability
from wybra.secrets.config import SecretsSettings
from wybra.secrets.sources import (
    AwsSecretsManagerSourceDriver,
    EnvironmentSecretSourceDriver,
    KeychainSecretSourceDriver,
    VaultSecretSourceDriver,
)
from wybra.services.crypto import SecretEnvelopeService
from wybra.tools.app_startup import (
    config_source_from_click_context,
)
from wybra.tools.cli_logging import configure_cli_logging
from wybra.tools.project import ProjectToolConfigurationError, runtime_project_root

from .args import REVOKE_ALL_PASSKEYS, AuthmgrArgs
from .output import (
    _print_failure,
    _print_records,
    _print_single_record,
    _print_user_records,
)
from .passwords import PasswordSourceError, _read_password, _read_required_password
from .schema import verify_identity_schema_for_database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AuthmgrCommandSettings:
    auth: AuthSettings
    secret_service: SecretEnvelopeService


def _run_authmgr(ctx: click.Context, args: AuthmgrArgs) -> None:
    try:
        exit_code = asyncio.run(
            _main_async(args, config_source=_config_source_from_context(ctx))
        )
    except PasswordSourceError as exc:
        raise click.BadParameter(str(exc), param_hint=["--password"]) from exc
    except (ConfigurationError, LoggingConfigurationError) as exc:
        logger.error("configuration: %s", exc)
        exit_code = 1
    except click.Abort:
        raise
    ctx.exit(exit_code)


async def _main_async(args: AuthmgrArgs, *, config_source: str | None = None) -> int:
    settings = _load_command_settings_for_command(config_source=config_source)
    database = create_database(settings.auth.database_url)
    try:
        await verify_identity_schema_for_database(database)
        async with auth_persistence_session(
            database,
            secret_service=settings.secret_service,
        ) as scope:
            return await _run_command(scope, settings.auth, args)
    finally:
        await close_database(database)


async def _run_command(
    scope: AuthPersistenceScope,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        logger.error("%s: not implemented", args.command)
        return 1
    return await handler(scope, settings, args)


async def _handle_user_create(
    scope: AuthPersistenceScope,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    management = scope.management
    if args.add_groups:
        group_result = await management.validate_group_targets(args.add_groups)
        if group_result.is_failure():
            return _print_failure(group_result.error_type, group_result.message)

    password = _read_required_password(args.password)
    result = await management.create_local_user(
        settings.identity_options,
        email=args.email,
        password=password,
        is_admin=args.admin,
        is_superuser=args.superuser,
        is_verified=not args.unverified,
        preferred_timezone=args.preferred_timezone,
        expires_at=args.expires_at,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    if args.add_groups:
        result = await management.update_user_groups(
            target=args.email,
            set_group_targets=args.add_groups,
        )
        if result.is_failure():
            return _print_failure(result.error_type, result.message)

    value = result.value or {}
    totp_value: dict[str, Any] | None = None
    if args.totp:
        totp_result = await management.provision_totp(
            settings.identity_options,
            target=args.email,
        )
        if totp_result.is_failure():
            return _print_failure(totp_result.error_type, totp_result.message)
        totp_value = totp_result.value or {}

    payload = _user_operation_payload(value, totp_value=totp_value)

    if args.json_output:
        _print_user_operation_json(
            payload,
            include_secrets=args.include_secrets,
        )
        return 0

    print(f"created user: {payload['user'].get('email', args.email)}")
    _print_totp_material(payload)
    return 0


async def _handle_user_update(
    scope: AuthPersistenceScope,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    management = scope.management
    password = _read_password(args.password) if args.password is not None else None
    result: Result[dict[str, Any]] = Result.ok({})
    if _user_metadata_update_requested(args, password):
        result = await management.update_local_user(
            settings.identity_options,
            target=args.target,
            is_admin=args.is_admin,
            is_superuser=args.is_superuser,
            is_verified=args.is_verified,
            password=password,
            revoke_sessions=not args.no_revoke,
            preferred_timezone=args.preferred_timezone,
            clear_preferred_timezone=args.clear_preferred_timezone,
            expires_at=args.expires_at,
            clear_expires_at=args.no_expires_at,
        )
        if result.is_failure():
            return _print_failure(result.error_type, result.message)
    else:
        if not (
            args.add_groups
            or args.remove_groups
            or args.set_groups
            or _user_totp_operation_requested(args)
            or _user_passkey_operation_requested(args)
        ):
            return _print_failure(ERROR_NO_CHANGES, "No user changes were requested.")
        result = await _resolve_target_record(management, args.target)
        if result.is_failure():
            return _print_failure(result.error_type, result.message)

    membership_result = await _update_user_groups_from_args(management, args)
    if membership_result.is_failure():
        return _print_failure(
            membership_result.error_type,
            membership_result.message,
        )

    value = {
        **(result.value or {}),
        **(membership_result.value or {}),
    }
    totp_value = await _update_user_totp_from_args(management, settings, args)
    if totp_value.is_failure():
        return _print_failure(totp_value.error_type, totp_value.message)
    passkey_value = await _update_user_passkeys_from_args(management, args)
    if passkey_value.is_failure():
        return _print_failure(passkey_value.error_type, passkey_value.message)
    payload = _user_operation_payload(
        value,
        totp_value=totp_value.value if totp_value.value else None,
        passkey_value=passkey_value.value if passkey_value.value else None,
    )

    if args.json_output:
        _print_user_operation_json(
            payload,
            include_secrets=args.include_secrets,
        )
        return 0

    print(f"updated user: {payload['user'].get('email', args.target)}")
    _print_totp_material(payload)
    _print_passkey_revocation(payload)
    return 0


async def _handle_user_delete(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    management = scope.management
    if not args.force:
        target_result = await _resolve_target_record(management, args.target)
        if target_result.is_failure():
            return _print_failure(target_result.error_type, target_result.message)
        target_record = target_result.value or {}
        if not _confirm_destructive("delete", target_record):
            return 1

    result = await management.delete_local_user(target=args.target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted user: {value.get('email', args.target)}")
    return 0


async def _handle_user_deactivate(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    management = scope.management
    if not args.force:
        target_result = await _resolve_target_record(management, args.target)
        if target_result.is_failure():
            return _print_failure(target_result.error_type, target_result.message)
        target_record = target_result.value or {}
        if not _confirm_destructive("deactivate", target_record):
            return 1

    result = await management.deactivate_local_user(target=args.target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deactivated user: {value.get('email', args.target)}")
    return 0


async def _handle_user_list(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.list_local_users(
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
        include_passkeys=args.include_passkeys,
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
    scope: AuthPersistenceScope,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    password = _read_required_password(args.password)
    result = await scope.management.update_local_user(
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
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.create_scope(
        scope=args.scope,
        description=args.description,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"created scope: {value.get('scope', args.scope)}")
    return 0


async def _handle_scope_update(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.update_scope(
        scope=args.scope,
        description=args.description,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"updated scope: {value.get('scope', args.scope)}")
    return 0


async def _handle_scope_delete(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.delete_scope(scope=args.scope)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted scope: {value.get('scope', args.scope)}")
    return 0


async def _handle_scope_list(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.list_scopes()
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
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    management = scope.management
    result = await management.create_group(
        abbrev=args.group_target,
        description=args.description or "",
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    for scope_name in args.add_scopes:
        result = await management.add_scope_to_group(
            group_target=args.group_target,
            scope=scope_name,
        )
        if result.is_failure():
            return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"created group: {value.get('abbrev', args.group_target)}")
    return 0


async def _handle_group_list(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.list_groups()
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
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.get_group(target=args.group_target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    _print_single_record(result.value or {}, json_output=args.json_output)
    return 0


async def _handle_group_update(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await _update_group_from_args(scope.management, args)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"updated group: {value.get('abbrev', args.group_target)}")
    return 0


async def _handle_group_delete(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.delete_group(target=args.group_target)
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    value = result.value or {}
    print(f"deleted group: {value.get('abbrev', args.group_target)}")
    return 0


async def _handle_group_add_user(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.add_user_to_group(
        group_target=args.group_target,
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"added user to group: {args.group_target}")
    return 0


async def _handle_group_remove_user(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.remove_user_from_group(
        group_target=args.group_target,
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"removed user from group: {args.group_target}")
    return 0


async def _handle_group_add_group(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.add_child_group_to_group(
        parent_target=args.group_target,
        child_target=args.child_group_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"added child group: {args.group_target}")
    return 0


async def _handle_group_remove_group(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.remove_child_group_from_group(
        parent_target=args.group_target,
        child_target=args.child_group_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    print(f"removed child group: {args.group_target}")
    return 0


async def _handle_group_effective_scopes(
    scope: AuthPersistenceScope,
    _settings: AuthSettings,
    args: AuthmgrArgs,
) -> int:
    result = await scope.management.effective_scopes_for_user(
        user_target=args.user_target,
    )
    if result.is_failure():
        return _print_failure(result.error_type, result.message)

    _print_single_record(result.value or {}, json_output=args.json_output)
    return 0


CommandHandler = Callable[
    [AuthPersistenceScope, AuthSettings, AuthmgrArgs],
    Awaitable[int],
]

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


def _load_command_settings_for_command(
    config_source: str | None = None,
) -> _AuthmgrCommandSettings:
    try:
        configure_cli_logging()
        project_root = runtime_project_root()
        app_config = load_app_config(
            project_root=project_root,
            config_path=_config_source_path(config_source),
        )
        configure_cli_logging(app_config)
    except ProjectToolConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc
    except CompositionError as exc:
        raise ConfigurationError(
            f"{str(exc).rstrip('.')}. Pass --config or set APP_CONFIG."
        ) from exc

    return _AuthmgrCommandSettings(
        auth=AuthSettings.load_settings(
            ConfigService(
                [AppConfigSource(app_config)],
                config_defs=(RUNTIME_CONFIG_DEF, AuthSettings.module_config),
                discover_module_config=False,
            ),
            app_config=app_config,
        ),
        secret_service=_secret_envelope_service_for_command(app_config),
    )


def _secret_envelope_service_for_command(
    app_config: AppConfig,
) -> SecretEnvelopeService:
    settings = _secrets_settings_for_app_config(app_config)
    if settings.crypto.source is None:
        return SecretEnvelopeService.from_env(os.environ)

    secrets = DefaultSecretsCapability.from_drivers(
        (
            EnvironmentSecretSourceDriver(os.environ),
            AwsSecretsManagerSourceDriver(settings.kms),
            KeychainSecretSourceDriver(settings.keychain),
            VaultSecretSourceDriver(settings.vault),
        )
    )
    return SecretEnvelopeService.from_secrets(
        secrets,
        source=settings.crypto.source,
        current_key=settings.crypto.current_key,
        previous_keys=settings.crypto.previous_keys,
    )


def _secrets_settings_for_app_config(app_config: AppConfig) -> SecretsSettings:
    config = ConfigService(
        [AppConfigSource(app_config)],
        config_defs=(SecretsSettings.module_config,),
        discover_module_config=False,
    )
    return SecretsSettings.load_settings(config)


def _config_source_from_context(ctx: click.Context) -> str | None:
    return config_source_from_click_context(
        ctx,
        error_factory=ConfigurationError,
        invalid_type_message=lambda value_type: (
            "Invalid Click context for authmgr: config_source must be a string, "
            f"got {value_type.__name__!r}."
        ),
    )


def _config_source_path(config_source: str | None) -> Path | None:
    if config_source is None:
        return None
    return Path(config_source)


async def _resolve_target_record(
    management: AuthManagementStore,
    target: str,
) -> Result[dict[str, Any]]:
    return await management.resolve_user_record(target)


def _confirm_destructive(action: str, record: dict[str, Any]) -> bool:
    answer = input(
        f"{action} {record['email']} ({record['id']})? Type 'yes' to continue: "
    )
    return answer == "yes"


async def _update_group_from_args(
    management: AuthManagementStore,
    args: AuthmgrArgs,
) -> Result[dict[str, Any]]:
    result: Result[dict[str, Any]] | None = None
    if args.description is not None:
        result = await management.update_group(
            target=args.group_target,
            description=args.description,
        )
        if result.is_failure():
            return result

    for scope_name in args.add_scopes:
        result = await management.add_scope_to_group(
            group_target=args.group_target,
            scope=scope_name,
        )
        if result.is_failure():
            return result

    for scope_name in args.remove_scopes:
        result = await management.remove_scope_from_group(
            group_target=args.group_target,
            scope=scope_name,
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
            args.preferred_timezone,
            args.expires_at,
        )
    ) or any(
        (
            args.clear_preferred_timezone,
            args.no_expires_at,
        )
    )


def _user_totp_operation_requested(args: AuthmgrArgs) -> bool:
    return args.totp or args.no_totp or args.rcodes


def _user_passkey_operation_requested(args: AuthmgrArgs) -> bool:
    return args.revoke_passkey is not None


async def _update_user_totp_from_args(
    management: AuthManagementStore,
    settings: AuthSettings,
    args: AuthmgrArgs,
) -> Result[dict[str, Any]]:
    if not _user_totp_operation_requested(args):
        return Result.ok({})

    if args.totp:
        return await management.provision_totp(
            settings.identity_options,
            target=args.target,
        )
    if args.no_totp:
        return await management.disable_totp(target=args.target)

    return await management.rotate_totp_recovery_codes(target=args.target)


async def _update_user_passkeys_from_args(
    management: AuthManagementStore,
    args: AuthmgrArgs,
) -> Result[dict[str, Any]]:
    if not _user_passkey_operation_requested(args):
        return Result.ok({})

    return await management.revoke_passkeys(
        target=args.target,
        credential=_passkey_credential_from_args(args),
    )


def _passkey_credential_from_args(args: AuthmgrArgs) -> str | None:
    if args.revoke_passkey == REVOKE_ALL_PASSKEYS:
        return None
    return args.revoke_passkey


def _user_operation_payload(
    user_record_value: dict[str, Any],
    *,
    totp_value: dict[str, Any] | None,
    passkey_value: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    payload = {
        "user": _operation_user_record(
            user_record_value,
            totp_value,
            passkey_value,
        )
    }
    totp_material = _operation_totp_material(totp_value)
    if totp_material is not None:
        payload["totp"] = totp_material
    passkeys = _operation_passkeys(passkey_value)
    if passkeys is not None:
        payload["passkeys"] = {"revoked": passkeys}
    return payload


def _operation_user_record(
    user_record_value: dict[str, Any],
    totp_value: dict[str, Any] | None,
    passkey_value: dict[str, Any] | None,
) -> dict[str, Any]:
    for operation_value in (passkey_value, totp_value):
        if operation_value is None:
            continue
        candidate = operation_value.get("user")
        if isinstance(candidate, dict):
            return candidate
    return user_record_value


def _operation_totp_material(
    totp_value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if totp_value is None:
        return None
    candidate = totp_value.get("totp")
    return candidate if isinstance(candidate, dict) else None


def _operation_passkeys(
    passkey_value: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    if passkey_value is None:
        return None
    candidate = passkey_value.get("passkeys")
    return candidate if isinstance(candidate, list) else None


def _print_user_operation_json(
    payload: dict[str, dict[str, Any]],
    *,
    include_secrets: bool,
) -> None:
    json_payload = {"user": payload["user"]}
    totp_material = payload.get("totp")
    if totp_material is not None:
        json_payload["totp"] = _totp_json_payload(
            totp_material,
            include_secrets=include_secrets,
        )
    passkey_material = payload.get("passkeys")
    if passkey_material is not None:
        json_payload["passkeys"] = passkey_material
    print(json.dumps(json_payload))


def _totp_json_payload(
    totp_value: dict[str, Any],
    *,
    include_secrets: bool,
) -> dict[str, Any]:
    if include_secrets:
        return totp_value

    secret, provisioning_uri, recovery_codes = _totp_material_fields(totp_value)
    payload: dict[str, Any] = {}
    if secret is not None or provisioning_uri is not None:
        payload["provisioned"] = True
    if recovery_codes:
        payload["recovery_codes_generated"] = True
    return payload


def _print_totp_material(
    payload: dict[str, dict[str, Any]],
    *,
    stream: TextIO | None = None,
) -> None:
    totp_value = payload.get("totp")
    if totp_value is None:
        return
    secret, provisioning_uri, recovery_codes = _totp_material_fields(totp_value)
    if secret is None and provisioning_uri is None and not recovery_codes:
        return

    output = sys.stderr if stream is None else stream
    print("Operator credential material. Store and transmit it securely.", file=output)
    if secret is not None:
        _write_credential_material(output, f"TOTP secret: {secret}")
    if provisioning_uri is not None:
        _write_credential_material(output, f"TOTP provisioning URI: {provisioning_uri}")
    if recovery_codes:
        print("Recovery codes:", file=output)
        for recovery_code in recovery_codes:
            _write_credential_material(output, f"- {recovery_code}")


def _print_passkey_revocation(
    payload: dict[str, dict[str, Any]],
    *,
    stream: TextIO | None = None,
) -> None:
    passkey_value = payload.get("passkeys")
    if passkey_value is None:
        return
    revoked = passkey_value.get("revoked")
    if not isinstance(revoked, list):
        return

    output = sys.stdout if stream is None else stream
    print(f"revoked passkeys: {len(revoked)}", file=output)
    for passkey in revoked:
        if not isinstance(passkey, dict):
            continue
        print(f"- {passkey.get('id', '<unknown>')}", file=output)


def _write_credential_material(output: TextIO, line: str) -> None:
    # Intentional CLI stream output for explicitly requested one-time operator
    # credential handoff. This is not diagnostic logging.
    output.write(f"{line}\n")


def _totp_material_fields(
    totp_value: dict[str, Any],
) -> tuple[Any, Any, tuple[Any, ...]]:
    secret = totp_value.get("secret")
    provisioning_uri = totp_value.get("provisioning_uri")
    recovery_codes = tuple(totp_value.get("recovery_codes") or ())
    return secret, provisioning_uri, recovery_codes


async def _update_user_groups_from_args(
    management: AuthManagementStore,
    args: AuthmgrArgs,
) -> Result[dict[str, Any]]:
    if not (args.add_groups or args.remove_groups or args.set_groups):
        return Result.ok({})

    return await management.update_user_groups(
        target=args.target,
        add_group_targets=args.add_groups,
        remove_group_targets=args.remove_groups,
        set_group_targets=args.set_groups,
    )
