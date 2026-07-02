from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import click

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.composition import (
    APP_CONFIG_ENV,
    CompositionError,
    raw_config_sections,
)
from wybra.core.exceptions import ConfigurationError
from wybra.secrets.config import KeychainSecretSourceSettings, SecretsSettings
from wybra.secrets.keys import KnownSecretKey, known_keychain_secret_keys
from wybra.secrets.sources import KeychainSecretSourceDriver
from wybra.services.secrets import secret_key_value
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    normalise_cli_config_source,
)
from wybra.tools.project import ProjectToolConfigurationError, runtime_project_root

PROGRAM_NAME = "wybra-secret"
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 120}


@dataclass(frozen=True, slots=True)
class SecretCommandSettings:
    keychain: KeychainSecretSourceSettings
    known_keys: tuple[KnownSecretKey, ...]


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
    key: str | None,
    value: str | None,
    stdin_source: bool,
    prompt_source: bool,
    json_input: bool,
) -> None:
    """Store one or more values in the OS keychain."""

    driver = _keychain_driver_from_context(ctx)
    if json_input:
        if key is not None or value is not None or stdin_source or prompt_source:
            raise click.UsageError(
                "--json cannot be combined with KEY, VALUE, --stdin, or --prompt."
            )
        stored = _store_json_values(driver, sys.stdin)
        _write_json({"stored": [_stored_payload(driver, item) for item in stored]})
        return

    if key is None:
        raise click.UsageError("Missing argument 'KEY'.")
    secret_value = _secret_value_from_input(
        key=key,
        value=value,
        stdin_source=stdin_source,
        prompt_source=prompt_source,
    )
    _store_secret(driver, key, secret_value)
    click.echo(f"Stored {secret_key_value(key)}.")


@secret_command.command(name="get")
@click.option("--json", "json_output", is_flag=True, help="Render JSON output.")
@click.argument("key")
@click.pass_context
def get_command(ctx: click.Context, key: str, json_output: bool) -> None:
    """Read one value from the OS keychain."""

    driver = _keychain_driver_from_context(ctx)
    key_value = secret_key_value(key)
    try:
        secret = driver.resolve(key_value).reveal()
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        service, username = driver.identity(key_value)
        _write_json(
            {
                "key": key_value,
                "service": service,
                "username": username,
                "value": secret,
            }
        )
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
        _write_json({"keys": records})
        return
    for record in records:
        state = "present" if record["exists"] else "missing"
        click.echo(
            f"{state}\t{record['key']}\t{record['owner']}\t{record['description']}"
        )


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
        print("Aborted!", file=sys.stderr)
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
        raw_config = _load_optional_raw_config(config_source)
        if raw_config is None:
            secrets_settings = SecretsSettings()
            known_keys = known_keychain_secret_keys()
        else:
            secrets_settings = _secrets_settings_from_raw_config(raw_config)
            known_keys = known_keychain_secret_keys(
                raw_config=raw_config,
                secrets_settings=secrets_settings,
            )
        return SecretCommandSettings(
            keychain=secrets_settings.keychain,
            known_keys=known_keys,
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


__all__ = ("main", "secret_command")
