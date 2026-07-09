from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import click
from tortoise.transactions import in_transaction

from wybra.auth.settings import AuthSettings, load_auth_settings
from wybra.config import AppConfigSource, ConfigService, MappingConfigSource
from wybra.core.composition import (
    APP_CONFIG_ENV,
    CompositionError,
    load_app_config,
    raw_config_sections,
)
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.exceptions import ConfigurationError
from wybra.core.logging import merge_logging_config
from wybra.db.persistence import close_database, create_database
from wybra.forms.rotation import plan_csrf_token_secret_rotation
from wybra.forms.settings import FormsSettings
from wybra.secrets.config import KeychainSecretSourceSettings, SecretsSettings
from wybra.secrets.keys import (
    KnownSecretKey,
    builtin_keychain_secret_key,
    known_keychain_secret_keys,
    normalise_secret_key_type,
)
from wybra.secrets.reencryption import (
    ReencryptSecretsResult,
    reencrypt_persisted_secrets,
)
from wybra.secrets.sources import KeychainSecretSourceDriver
from wybra.services.crypto import (
    SecretEnvelopeService,
    SecretKeyRotationPlan,
    parse_secret_key_bundle,
    plan_secret_key_rotation,
)
from wybra.services.secrets import (
    KEYCHAIN_SOURCE,
    MissingSecretError,
    secret_key_value,
)
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    normalise_cli_config_source,
)
from wybra.tools.cli_logging import configure_cli_logging
from wybra.tools.project import ProjectToolConfigurationError, runtime_project_root

PROGRAM_NAME = "wybra-secret"
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 120}


@dataclass(frozen=True, slots=True)
class SecretCommandSettings:
    keychain: KeychainSecretSourceSettings
    secrets: SecretsSettings
    known_keys: tuple[KnownSecretKey, ...]
    raw_config: Mapping[str, Mapping[str, Any]] | None = None


@click.group(
    name=PROGRAM_NAME,
    context_settings=CONTEXT_SETTINGS,
    help="Manage Wybra secrets stored in the operating system keychain.",
)
@click.option(CONFIG_SOURCE_OPTION, CONFIG_SOURCE_CONTEXT_KEY, help=CONFIG_SOURCE_HELP)
@click.pass_context
def secret_command(ctx: click.Context, config_source: str | None) -> None:
    ctx.obj = {CONFIG_SOURCE_CONTEXT_KEY: config_source}


@secret_command.command(name="set")
@click.option(
    "--type",
    "key_type",
    help="Use a built-in Wybra key type instead of a raw key.",
)
@click.option(
    "--dev",
    "development",
    is_flag=True,
    help="Use the development variant for a built-in key type.",
)
@click.option(
    "--stdin",
    "stdin_source",
    is_flag=True,
    help="Read the secret value from stdin.",
)
@click.option(
    "--prompt",
    "prompt_source",
    is_flag=True,
    help="Read the secret value through a hidden prompt.",
)
@click.option(
    "--json",
    "json_input",
    is_flag=True,
    help="Read a JSON object of key/value pairs from stdin.",
)
@click.argument("key", required=False)
@click.argument("value", required=False)
@click.pass_context
def set_command(
    ctx: click.Context,
    key_type: str | None,
    development: bool,
    key: str | None,
    value: str | None,
    stdin_source: bool,
    prompt_source: bool,
    json_input: bool,
) -> None:
    """Store one or more values in the OS keychain."""

    driver = _keychain_driver_from_context(ctx)
    if json_input:
        if (
            key_type is not None
            or development
            or key is not None
            or value is not None
            or stdin_source
            or prompt_source
        ):
            raise click.UsageError(
                "--json cannot be combined with --type, --dev, KEY, VALUE, "
                "--stdin, or --prompt."
            )
        stored = _store_json_values(driver, sys.stdin)
        _write_json({"stored": [_stored_payload(driver, item) for item in stored]})
        return

    resolved_key, resolved_value = _set_key_and_value(
        key=key,
        value=value,
        key_type=key_type,
        development=development,
    )
    if resolved_key is None:
        raise click.UsageError("Missing argument 'KEY'.")
    secret_value = _secret_value_from_input(
        key=resolved_key,
        value=resolved_value,
        stdin_source=stdin_source,
        prompt_source=prompt_source,
    )
    _store_secret(driver, resolved_key, secret_value)
    click.echo(f"Stored {secret_key_value(resolved_key)}.")


@secret_command.command(name="get")
@click.option(
    "--type",
    "key_type",
    help="Use a built-in Wybra key type instead of a raw key.",
)
@click.option(
    "--dev",
    "development",
    is_flag=True,
    help="Use the development variant for a built-in key type.",
)
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.argument("key", required=False)
@click.pass_context
def get_command(
    ctx: click.Context,
    key_type: str | None,
    development: bool,
    key: str | None,
    json_output: bool,
) -> None:
    """Read one value from the OS keychain."""

    driver = _keychain_driver_from_context(ctx)
    key_value = secret_key_value(
        _get_key(key=key, key_type=key_type, development=development)
    )
    try:
        secret = driver.resolve(key_value).reveal()
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        service, username = driver.identity(key_value)
        payload = {
            "key": key_value,
            "service": service,
            "username": username,
            "value": secret,
        }
        if key_type is not None:
            payload["name"] = normalise_secret_key_type(key_type)
        _write_json(payload)
        return
    click.echo(secret)


@secret_command.command(name="list")
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.pass_context
def list_command(ctx: click.Context, json_output: bool) -> None:
    """List Wybra-known key references and whether they exist."""

    settings = _secret_command_settings_from_context(ctx)
    driver = KeychainSecretSourceDriver(settings.keychain)
    records = [
        _known_key_record(driver, known_key) for known_key in settings.known_keys
    ]
    if json_output:
        _write_json({"keys": {str(record["name"]): record for record in records}})
        return
    for record in records:
        state = "present" if record["exists"] else "missing"
        click.echo(
            f"{state}\t{record['name']}\t{record['key']}\t"
            f"{record['owner']}\t{record['description']}"
        )


@secret_command.group(name="rotate")
def rotate_command() -> None:
    """Rotate supported Wybra keychain-backed secrets."""


@rotate_command.command(name="secret-key")
@click.option("--dry-run", is_flag=True, help="Validate and report without writing.")
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.pass_context
def rotate_secret_key_command(
    ctx: click.Context,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Rotate the keychain-backed [secrets.crypto] system secret key."""

    settings = _secret_command_settings_from_context(ctx)
    crypto = settings.secrets.crypto
    if crypto.source != KEYCHAIN_SOURCE:
        raise click.ClickException(
            "Secret-key rotation is limited to keychain-backed system secret keys."
        )
    if crypto.previous_keys is None:
        raise click.ClickException(
            "Secret-key rotation requires [secrets.crypto].previous_keys."
        )

    driver = KeychainSecretSourceDriver(settings.keychain)
    current = _resolve_required_secret(driver, crypto.current_key, "current secret key")
    previous = _resolve_optional_secret(driver, crypto.previous_keys)
    try:
        plan = plan_secret_key_rotation(current=current, previous=previous)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if not dry_run:
        _store_secret(driver, crypto.previous_keys, plan.previous_value)
        _store_secret(driver, crypto.current_key, plan.current_value)
        _validate_secret_key_rotation(
            driver,
            crypto.current_key,
            crypto.previous_keys,
            plan,
        )

    payload = {
        "target": "secret-key",
        "dry_run": dry_run,
        "current_key": crypto.current_key,
        "previous_keys": crypto.previous_keys,
        "old_current_version": plan.retired_version,
        "new_current_version": plan.new_version,
        "previous_key_count": plan.previous_key_count,
    }
    _write_rotation_result(payload, json_output=json_output)


@rotate_command.command(name="csrf-token-secret")
@click.option("--dry-run", is_flag=True, help="Validate and report without writing.")
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.pass_context
def rotate_csrf_token_secret_command(
    ctx: click.Context,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Rotate the keychain-backed forms CSRF token signing secret."""

    settings = _secret_command_settings_from_context(ctx)
    forms_settings = _forms_settings_from_command_settings(settings)
    current_reference = forms_settings.csrf_token_secret_reference
    previous_reference = forms_settings.csrf_token_secret_previous_reference
    if current_reference is None or current_reference[0] != KEYCHAIN_SOURCE:
        raise click.ClickException(
            "CSRF token-secret rotation is limited to keychain-backed CSRF token "
            "secrets."
        )
    if previous_reference is None or previous_reference[0] != KEYCHAIN_SOURCE:
        raise click.ClickException(
            "CSRF token-secret rotation requires a keychain-backed previous "
            "CSRF token secret reference."
        )

    driver = KeychainSecretSourceDriver(settings.keychain)
    _current_source, current_key = current_reference
    _previous_source, previous_key = previous_reference
    current = _resolve_required_secret(driver, current_key, "current CSRF token secret")
    previous = _resolve_optional_secret(driver, previous_key)
    try:
        plan = plan_csrf_token_secret_rotation(current=current, previous=previous)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if not dry_run:
        _store_secret(driver, previous_key, plan.previous_value)
        _store_secret(driver, current_key, plan.current_value)
        _validate_csrf_token_secret_rotation(
            driver,
            current_key,
            previous_key,
            plan.current_value,
            plan.previous_value,
        )

    payload = {
        "target": "csrf-token-secret",
        "dry_run": dry_run,
        "current_key": current_key,
        "previous_key": previous_key,
        "previous_secret_count": plan.previous_secret_count,
    }
    _write_rotation_result(payload, json_output=json_output)


@secret_command.command(name="reencrypt-secrets")
@click.option("--dry-run", is_flag=True, help="Validate and report without writing.")
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.pass_context
def reencrypt_secrets_command(
    ctx: click.Context,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Re-encrypt reversible persisted secret envelopes with the current key."""

    try:
        result = asyncio.run(_reencrypt_secrets(ctx, dry_run=dry_run))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    _write_reencrypt_result(result, json_output=json_output)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = secret_command.main(
            args=None if argv is None else list(argv),
            prog_name=PROGRAM_NAME,
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


def _keychain_driver_from_context(ctx: click.Context) -> KeychainSecretSourceDriver:
    return KeychainSecretSourceDriver(
        _secret_command_settings_from_context(ctx).keychain
    )


def _secret_command_settings_from_context(ctx: click.Context) -> SecretCommandSettings:
    root_context = ctx.find_root()
    config_source = _config_source_from_context(root_context)
    try:
        configure_cli_logging()
        raw_config = _load_optional_raw_config(config_source)
        if raw_config is None:
            secrets_settings = SecretsSettings()
            known_keys = known_keychain_secret_keys()
        else:
            configure_cli_logging(config=merge_logging_config(raw_config.get("log")))
            secrets_settings = _secrets_settings_from_raw_config(raw_config)
            known_keys = known_keychain_secret_keys(
                raw_config=raw_config,
                secrets_settings=secrets_settings,
            )
        return SecretCommandSettings(
            keychain=secrets_settings.keychain,
            secrets=secrets_settings,
            known_keys=known_keys,
            raw_config=raw_config,
        )
    except (CompositionError, ConfigurationError, ProjectToolConfigurationError) as exc:
        raise click.ClickException(str(exc)) from exc


def _config_source_from_context(ctx: click.Context) -> str | None:
    obj = ctx.obj
    if not isinstance(obj, Mapping):
        return None
    config_source = obj.get(CONFIG_SOURCE_CONTEXT_KEY)
    if config_source is None:
        return None
    if not isinstance(config_source, str):
        raise click.UsageError(
            f"--config must be a string, got {type(config_source).__name__}."
        )
    try:
        return normalise_cli_config_source(config_source)
    except ProjectToolConfigurationError as exc:
        raise click.UsageError(str(exc)) from exc


def _load_optional_raw_config(
    config_source: str | None,
) -> Mapping[str, Mapping[str, Any]] | None:
    environment = dict(os.environ)
    if config_source is not None:
        environment[APP_CONFIG_ENV] = config_source

    if APP_CONFIG_ENV not in environment:
        return None
    if not environment[APP_CONFIG_ENV].strip():
        raise click.UsageError(f"{APP_CONFIG_ENV} must not be blank.")

    config_path = _config_path(environment[APP_CONFIG_ENV])
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CompositionError(f"App config {config_path} could not be read.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CompositionError(f"App config {config_path} is invalid: {exc}") from exc

    if not isinstance(data, Mapping):
        raise CompositionError(f"App config {config_path} must be a TOML table.")
    return raw_config_sections(data)


def _config_path(config_source: str) -> Path:
    path = Path(config_source)
    if not path.is_absolute():
        path = runtime_project_root() / path
    return path.resolve()


def _secrets_settings_from_raw_config(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> SecretsSettings:
    config = ConfigService(
        [MappingConfigSource(raw_config)],
        config_defs=(SecretsSettings.module_config,),
        discover_module_config=False,
    )
    return SecretsSettings.load_settings(config)


def _forms_settings_from_command_settings(
    settings: SecretCommandSettings,
) -> FormsSettings:
    if settings.raw_config is None:
        raise click.ClickException(
            "CSRF token-secret rotation requires application configuration."
        )
    config = ConfigService(
        [MappingConfigSource(settings.raw_config)],
        config_defs=(FormsSettings.module_config,),
        discover_module_config=False,
    )
    return FormsSettings.load_settings(config)


def _auth_settings_from_context(ctx: click.Context) -> AuthSettings:
    config_source = _config_source_from_context(ctx.find_root())
    try:
        app_config = load_app_config(
            project_root=runtime_project_root(),
            config_path=Path(config_source) if config_source is not None else None,
        )
        configure_cli_logging(app_config)
    except ProjectToolConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc
    except CompositionError as exc:
        raise click.ClickException(
            f"{str(exc).rstrip('.')}. Pass --config or set {APP_CONFIG_ENV}."
        ) from exc
    try:
        config = ConfigService(
            [AppConfigSource(app_config)],
            config_defs=(RUNTIME_CONFIG_DEF, AuthSettings.module_config),
            discover_module_config=False,
        )
        return load_auth_settings(config, app_config=app_config)
    except ConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc


def _secret_envelope_service_from_command_settings(
    settings: SecretCommandSettings,
) -> SecretEnvelopeService:
    crypto = settings.secrets.crypto
    if crypto.source != KEYCHAIN_SOURCE:
        raise click.ClickException(
            "Persisted secret re-encryption requires keychain-backed [secrets.crypto]."
        )
    if crypto.previous_keys is None:
        raise click.ClickException(
            "Persisted secret re-encryption requires [secrets.crypto].previous_keys."
        )

    driver = KeychainSecretSourceDriver(settings.keychain)
    current = _resolve_required_secret(driver, crypto.current_key, "current secret key")
    previous = _resolve_optional_secret(driver, crypto.previous_keys)
    return SecretEnvelopeService.from_key_bundle(current, previous)


async def _reencrypt_secrets(
    ctx: click.Context,
    *,
    dry_run: bool,
) -> ReencryptSecretsResult:
    command_settings = _secret_command_settings_from_context(ctx)
    secret_service = _secret_envelope_service_from_command_settings(command_settings)
    auth_settings = _auth_settings_from_context(ctx)
    if auth_settings.database_connection is None:
        raise click.ClickException("Auth database connection is not configured.")
    database = await create_database(
        auth_settings.database_connection,
        modules=("wybra.auth",),
    )
    try:
        with database.context:
            async with in_transaction("default") as connection:
                return await reencrypt_persisted_secrets(
                    connection,
                    secret_service,
                    dry_run=dry_run,
                )
    finally:
        await close_database(database)


def _resolve_required_secret(
    driver: KeychainSecretSourceDriver,
    key: str,
    label: str,
) -> str:
    try:
        return driver.resolve(key).reveal()
    except MissingSecretError as exc:
        raise click.ClickException(
            f"Configured {label} is missing from the keychain: key={key}."
        ) from exc
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_optional_secret(
    driver: KeychainSecretSourceDriver,
    key: str,
) -> str | None:
    try:
        return driver.resolve(key).reveal()
    except MissingSecretError:
        return None
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _validate_secret_key_rotation(
    driver: KeychainSecretSourceDriver,
    current_key: str,
    previous_keys: str,
    plan: SecretKeyRotationPlan,
) -> None:
    current = _resolve_required_secret(driver, current_key, "current secret key")
    previous = _resolve_required_secret(driver, previous_keys, "previous secret keys")
    key_ring = parse_secret_key_bundle(current=current, previous=previous)
    if key_ring.current.version != plan.new_version:
        raise click.ClickException(
            "Post-rotation current secret key validation failed."
        )
    if plan.retired_version not in {key.version for key in key_ring.keys}:
        raise click.ClickException(
            "Post-rotation previous secret keys do not include the retired current key."
        )


def _validate_csrf_token_secret_rotation(
    driver: KeychainSecretSourceDriver,
    current_key: str,
    previous_key: str,
    expected_current: str,
    expected_previous: str,
) -> None:
    current = _resolve_required_secret(driver, current_key, "current CSRF token secret")
    previous = _resolve_required_secret(
        driver,
        previous_key,
        "previous CSRF token secrets",
    )
    if current != expected_current or previous != expected_previous:
        raise click.ClickException("Post-rotation CSRF token secret validation failed.")


def _set_key_and_value(
    *,
    key: str | None,
    value: str | None,
    key_type: str | None,
    development: bool,
) -> tuple[str | None, str | None]:
    if key_type is None:
        if development:
            raise click.UsageError("--dev requires --type.")
        return key, value
    if value is not None:
        raise click.UsageError("When --type is used, provide at most one VALUE.")
    return builtin_keychain_secret_key(key_type, development=development), key


def _get_key(
    *,
    key: str | None,
    key_type: str | None,
    development: bool,
) -> str:
    if key_type is None:
        if development:
            raise click.UsageError("--dev requires --type.")
        if key is None:
            raise click.UsageError("Missing argument 'KEY'.")
        return key
    if key is not None:
        raise click.UsageError("KEY cannot be combined with --type.")
    return builtin_keychain_secret_key(key_type, development=development)


def _secret_value_from_input(
    *,
    key: str,
    value: str | None,
    stdin_source: bool,
    prompt_source: bool,
) -> str:
    sources_selected = int(value is not None) + int(stdin_source) + int(prompt_source)
    if sources_selected > 1:
        raise click.UsageError("Choose only one secret value source.")
    if value is not None:
        return _non_empty_secret_value(value)
    if prompt_source:
        return _non_empty_secret_value(
            click.prompt(
                f"{secret_key_value(key)} value",
                hide_input=True,
                confirmation_prompt=True,
            )
        )
    return _non_empty_secret_value(sys.stdin.read().rstrip("\n"))


def _store_json_values(
    driver: KeychainSecretSourceDriver,
    stream: TextIO,
) -> tuple[str, ...]:
    try:
        payload = json.loads(stream.read())
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Invalid JSON input: {exc.msg}.") from exc
    if not isinstance(payload, dict):
        raise click.UsageError("--json input must be an object of key/value pairs.")

    stored: list[str] = []
    for key, value in payload.items():
        if not isinstance(key, str):
            raise click.UsageError("--json input keys must be strings.")
        if not isinstance(value, str):
            raise click.UsageError(f"Secret value for {key!r} must be a string.")
        _store_secret(driver, key, value)
        stored.append(secret_key_value(key))
    return tuple(stored)


def _store_secret(
    driver: KeychainSecretSourceDriver,
    key: str,
    value: str,
) -> None:
    try:
        driver.store(secret_key_value(key), _non_empty_secret_value(value))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _non_empty_secret_value(value: str) -> str:
    if value == "":
        raise click.UsageError("Secret value must not be empty.")
    return value


def _known_key_record(
    driver: KeychainSecretSourceDriver,
    known_key: KnownSecretKey,
) -> dict[str, Any]:
    service, username = driver.identity(known_key.key)
    try:
        exists = driver.exists(known_key.key)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    return {
        "name": known_key.name,
        "key": known_key.key,
        "owner": known_key.owner,
        "description": known_key.description,
        "source": known_key.source,
        "required": known_key.required,
        "service": service,
        "username": username,
        "exists": exists,
    }


def _stored_payload(
    driver: KeychainSecretSourceDriver,
    key: str,
) -> dict[str, str]:
    service, username = driver.identity(key)
    return {"key": key, "service": service, "username": username}


def _write_json(payload: Mapping[str, Any]) -> None:
    click.echo(json.dumps(payload, sort_keys=True))


def _write_rotation_result(
    payload: Mapping[str, Any],
    *,
    json_output: bool,
) -> None:
    if json_output:
        _write_json(payload)
        return
    action = "Planned" if payload["dry_run"] else "Rotated"
    click.echo(f"{action} {payload['target']}.")


def _write_reencrypt_result(
    result: ReencryptSecretsResult,
    *,
    json_output: bool,
) -> None:
    payload = _reencrypt_result_payload(result)
    if json_output:
        _write_json(payload)
        return
    action = "Planned" if result.dry_run else "Re-encrypted"
    click.echo(
        f"{action} persisted secrets: scanned={result.scanned} "
        f"rewritten={result.rewritten} skipped_current={result.skipped_current} "
        f"skipped_plaintext={result.skipped_plaintext} "
        "unsupported_recovery_code_verifiers="
        f"{result.unsupported_recovery_code_verifiers}."
    )


def _reencrypt_result_payload(result: ReencryptSecretsResult) -> dict[str, Any]:
    return {
        "target": "reencrypt-secrets",
        "dry_run": result.dry_run,
        "scanned": result.scanned,
        "rewritten": result.rewritten,
        "skipped_current": result.skipped_current,
        "skipped_plaintext": result.skipped_plaintext,
        "unsupported_recovery_code_verifiers": (
            result.unsupported_recovery_code_verifiers
        ),
        "fields": [
            {
                "table": field.table,
                "field": field.field,
                "scanned": field.scanned,
                "rewritten": field.rewritten,
                "skipped_current": field.skipped_current,
                "skipped_plaintext": field.skipped_plaintext,
                "versions": [
                    {"version": version.version, "count": version.count}
                    for version in field.versions
                ],
            }
            for field in result.fields
        ],
    }


__all__ = ("main", "secret_command")
