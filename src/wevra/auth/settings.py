from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from wevra.auth.configuration import ConfigurationError
from wevra.auth.options import IdentityOptions
from wevra.auth.persistence.database import resolve_database_url
from wevra.core.composition import AppConfig

DATABASE_URL_ENV = "DATABASE_URL"
PASSWORD_SECTION_FIELD = "password"
PASSWORD_POLICY_SECTION_FIELD = "policy"
IDENTITY_OPTION_FIELDS = frozenset(
    {
        "account_creation_policy",
        "session_cookie_name",
        "session_lifetime_seconds",
        "session_cookie_force_secure",
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
AUTH_OPTION_FIELDS = IDENTITY_OPTION_FIELDS | {
    PASSWORD_SECTION_FIELD,
}


@dataclass(frozen=True, slots=True)
class AuthSettings:
    database_url: str
    identity_options: IdentityOptions = field(default_factory=IdentityOptions)


def load_auth_settings(
    *,
    app_config: AppConfig,
    environ: Mapping[str, str] | None = None,
) -> AuthSettings:
    env = os.environ if environ is None else environ
    auth_config = app_config.auth
    _reject_unknown_auth_options(auth_config)
    database_url = _configured_database_url(app_config, env)
    identity_options = _identity_options_from_auth_config(auth_config)

    return AuthSettings(
        database_url=resolve_database_url(
            database_url,
            app_config.config_path.resolve().parent,
        ),
        identity_options=identity_options,
    )


def _reject_unknown_auth_options(auth_config: Mapping[str, Any]) -> None:
    unknown_fields = sorted(set(auth_config) - AUTH_OPTION_FIELDS)
    if not unknown_fields:
        _reject_unknown_password_options(auth_config)
        return

    allowed_fields = ", ".join(sorted(AUTH_OPTION_FIELDS))
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
    app_config: AppConfig,
    env: Mapping[str, str],
) -> str:
    database_url = (
        _configured_env_value(env, DATABASE_URL_ENV) or app_config.database_url
    )

    if not isinstance(database_url, str) or not database_url.strip():
        raise ConfigurationError(
            "Application database_url must be configured as [app].database_url "
            "or DATABASE_URL."
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
