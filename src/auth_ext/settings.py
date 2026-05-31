from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auth_ext.configuration import ConfigurationError
from auth_ext.database import resolve_database_url
from auth_ext.options import IdentityOptions

AUTH_CONFIG_ENV = "AUTH_CONFIG"
AUTH_DATABASE_URL_ENV = "AUTH_DATABASE_URL"
DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_AUTH_CONFIG = Path("auth.toml")
DATABASE_URL_FIELD = "database_url"
PASSWORD_SECTION_FIELD = "password"
PASSWORD_POLICY_SECTION_FIELD = "policy"
IDENTITY_OPTION_FIELDS = frozenset(
    {
        "account_creation_policy",
        "session_cookie_name",
        "session_cookie_secure",
        "session_lifetime_seconds",
        "reset_password_token_secret",
        "verification_token_secret",
        "oauth_account_linking_enabled",
        "advanced_authentication_enabled",
    }
)
PASSWORD_POLICY_OPTION_MAP = {
    "minimum_length": "password_minimum_length",
    "minimum_strength": "password_minimum_strength",
    "minimum_character_categories": "password_minimum_character_categories",
    "common_fragments": "password_common_fragments",
}
PASSWORD_OPTION_FIELDS = frozenset({PASSWORD_POLICY_SECTION_FIELD})
AUTH_CONFIG_FIELDS = IDENTITY_OPTION_FIELDS | {
    DATABASE_URL_FIELD,
    PASSWORD_SECTION_FIELD,
}


@dataclass(frozen=True, slots=True)
class AuthSettings:
    database_url: str
    identity_options: IdentityOptions = field(default_factory=IdentityOptions)


def load_auth_settings(
    *,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AuthSettings:
    env = os.environ if environ is None else environ
    resolved_config_path = _configured_path(config_path, env)
    auth_config, base_path = _load_auth_toml(resolved_config_path)
    database_url = _configured_database_url(auth_config, env)
    identity_options = _identity_options_from_auth_config(auth_config)

    return AuthSettings(
        database_url=resolve_database_url(database_url, base_path),
        identity_options=identity_options,
    )


def _configured_path(
    config_path: str | Path | None,
    env: Mapping[str, str],
) -> Path | None:
    if config_path is not None:
        return Path(config_path)

    env_path = env.get(AUTH_CONFIG_ENV)
    if env_path:
        return Path(env_path)

    default_path = DEFAULT_AUTH_CONFIG
    return default_path if default_path.exists() else None


def _load_auth_toml(config_path: Path | None) -> tuple[dict[str, Any], Path]:
    if config_path is None:
        return {}, Path.cwd()

    if not config_path.exists():
        raise ConfigurationError(f"Auth config file does not exist: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Auth config file is invalid: {exc}") from exc

    auth_config = data.get("auth", {})
    if not isinstance(auth_config, dict):
        raise ConfigurationError("Auth config must contain an [auth] table.")

    _reject_unknown_auth_options(auth_config)
    return auth_config, config_path.resolve().parent


def _reject_unknown_auth_options(auth_config: Mapping[str, Any]) -> None:
    unknown_fields = sorted(set(auth_config) - AUTH_CONFIG_FIELDS)
    if not unknown_fields:
        _reject_unknown_password_options(auth_config)
        return

    allowed_fields = ", ".join(sorted(AUTH_CONFIG_FIELDS))
    unknown_list = ", ".join(unknown_fields)
    raise ConfigurationError(
        f"Unknown option(s) in [auth] configuration: {unknown_list}. "
        f"Allowed options are: {allowed_fields}."
    )


def _reject_unknown_password_options(auth_config: Mapping[str, Any]) -> None:
    password_config = auth_config.get(PASSWORD_SECTION_FIELD)
    if password_config is None:
        return

    if not isinstance(password_config, dict):
        raise ConfigurationError(
            "Auth password config must be an [auth.password] table."
        )

    unknown_fields = sorted(set(password_config) - PASSWORD_OPTION_FIELDS)
    if unknown_fields:
        unknown_list = ", ".join(unknown_fields)
        allowed_fields = ", ".join(sorted(PASSWORD_OPTION_FIELDS))
        raise ConfigurationError(
            f"Unknown option(s) in [auth.password] configuration: {unknown_list}. "
            f"Allowed options are: {allowed_fields}."
        )

    policy_config = password_config.get(PASSWORD_POLICY_SECTION_FIELD, {})
    if not isinstance(policy_config, dict):
        raise ConfigurationError(
            "Auth password policy config must be an [auth.password.policy] table."
        )

    unknown_policy_fields = sorted(set(policy_config) - set(PASSWORD_POLICY_OPTION_MAP))
    if unknown_policy_fields:
        unknown_list = ", ".join(unknown_policy_fields)
        allowed_fields = ", ".join(sorted(PASSWORD_POLICY_OPTION_MAP))
        raise ConfigurationError(
            "Unknown option(s) in [auth.password.policy] configuration: "
            f"{unknown_list}. Allowed options are: {allowed_fields}."
        )


def _configured_database_url(
    auth_config: Mapping[str, Any],
    env: Mapping[str, str],
) -> str:
    database_url = (
        _configured_env_value(env, AUTH_DATABASE_URL_ENV)
        or _configured_env_value(env, DATABASE_URL_ENV)
        or auth_config.get(DATABASE_URL_FIELD)
    )

    if not isinstance(database_url, str) or not database_url.strip():
        raise ConfigurationError(
            "Auth database_url must be configured as [auth].database_url, "
            "AUTH_DATABASE_URL, or DATABASE_URL."
        )

    return database_url


def _configured_env_value(env: Mapping[str, str], field_name: str) -> str | None:
    value = env.get(field_name)
    return value if value and value.strip() else None


def _identity_options_from_auth_config(
    auth_config: Mapping[str, Any],
) -> IdentityOptions:
    identity_kwargs = {
        key: value
        for key, value in auth_config.items()
        if key in IDENTITY_OPTION_FIELDS
    }
    identity_kwargs.update(_password_policy_options_from_auth_config(auth_config))
    return IdentityOptions(**identity_kwargs)


def _password_policy_options_from_auth_config(
    auth_config: Mapping[str, Any],
) -> dict[str, Any]:
    password_config = auth_config.get(PASSWORD_SECTION_FIELD, {})
    if not isinstance(password_config, dict):
        return {}

    policy_config = password_config.get(PASSWORD_POLICY_SECTION_FIELD, {})
    if not isinstance(policy_config, dict):
        return {}

    return {
        identity_option: policy_config[config_key]
        for config_key, identity_option in PASSWORD_POLICY_OPTION_MAP.items()
        if config_key in policy_config
    }
