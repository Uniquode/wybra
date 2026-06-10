from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

from envex import Env

from wevra.auth.configuration import ConfigurationError
from wevra.auth.options import (
    PASSKEY,
    PROVIDER,
    TOTP_MODE,
    IdentityIntegration,
    IdentityOptions,
    identity_env_setting_name,
    is_generate_local_identity_secret,
)
from wevra.auth.persistence.database import resolve_database_url
from wevra.core.composition import AppConfig
from wevra.core.settings import (
    EnvironmentSetting,
    env_setting_is_set,
    values_from_env_settings,
)

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
        TOTP_MODE,
        "provider_enabled",
        "passkey_enabled",
        "totp_allowed_drift",
        "totp_period_seconds",
        "totp_challenge_expiry_seconds",
        "totp_recovery_window_seconds",
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
ENV_ACCOUNT_CREATION_POLICY: Final = "ACCOUNT_CREATION_POLICY"
ENV_RESET_SECRET: Final = "RESET_SECRET"
ENV_SESSION_COOKIE: Final = "SESSION_COOKIE"
ENV_SESSION_FORCE_SECURE: Final = "SESSION_FORCE_SECURE"
ENV_SESSION_LIFETIME: Final = "SESSION_LIFETIME"
ENV_TOTP_MODE: Final = "TOTP_MODE"
ENV_TOTP_ALLOWED_DRIFT: Final = "TOTP_ALLOWED_DRIFT"
ENV_TOTP_PERIOD_SECONDS: Final = "TOTP_PERIOD_SECONDS"
ENV_TOTP_CHALLENGE_EXPIRY_SECONDS: Final = "TOTP_CHALLENGE_EXPIRY_SECONDS"
ENV_TOTP_RECOVERY_WINDOW_SECONDS: Final = "TOTP_RECOVERY_WINDOW_SECONDS"
ENV_VERIFICATION_SECRET: Final = "VERIFICATION_SECRET"


def _identity_env_settings() -> tuple[EnvironmentSetting, ...]:
    return (
        EnvironmentSetting(
            identity_env_setting_name(cast(IdentityIntegration, PROVIDER)),
            "provider_enabled",
            "bool",
        ),
        EnvironmentSetting(
            identity_env_setting_name(cast(IdentityIntegration, PASSKEY)),
            "passkey_enabled",
            "bool",
        ),
    )


IDENTITY_ENV_SETTINGS: Final[tuple[EnvironmentSetting, ...]] = (
    EnvironmentSetting(ENV_ACCOUNT_CREATION_POLICY, "account_creation_policy"),
    *_identity_env_settings(),
    EnvironmentSetting(ENV_RESET_SECRET, "reset_password_token_secret"),
    EnvironmentSetting(ENV_SESSION_COOKIE, "session_cookie_name"),
    EnvironmentSetting(ENV_SESSION_FORCE_SECURE, "session_cookie_force_secure", "bool"),
    EnvironmentSetting(ENV_SESSION_LIFETIME, "session_lifetime_seconds", "int"),
    EnvironmentSetting(ENV_TOTP_MODE, TOTP_MODE),
    EnvironmentSetting(ENV_TOTP_ALLOWED_DRIFT, "totp_allowed_drift", "int"),
    EnvironmentSetting(ENV_TOTP_PERIOD_SECONDS, "totp_period_seconds", "int"),
    EnvironmentSetting(
        ENV_TOTP_CHALLENGE_EXPIRY_SECONDS,
        "totp_challenge_expiry_seconds",
        "int",
    ),
    EnvironmentSetting(
        ENV_TOTP_RECOVERY_WINDOW_SECONDS,
        "totp_recovery_window_seconds",
        "int",
    ),
    EnvironmentSetting(ENV_VERIFICATION_SECRET, "verification_token_secret"),
)


@dataclass(frozen=True, slots=True)
class AuthSettings:
    database_url: str
    identity_options: IdentityOptions = field(default_factory=IdentityOptions)


def load_auth_settings(
    *,
    app_config: AppConfig,
    environ: Mapping[str, str] | None = None,
) -> AuthSettings:
    env = Env(
        environ=dict(environ or os.environ),
        readenv=False,
        update=False,
    )
    auth_config = app_config.auth
    _reject_unknown_auth_options(auth_config)
    database_url = _configured_database_url(app_config, env)
    identity_options = merge_identity_options_with_environment(
        _identity_options_from_auth_config(auth_config),
        auth_config,
        env,
    )

    return AuthSettings(
        database_url=resolve_database_url(
            database_url,
            app_config.config_path.resolve().parent,
        ),
        identity_options=identity_options,
    )


def merge_identity_options_with_environment(
    identity_options: IdentityOptions,
    auth_config: Mapping[str, Any],
    env: Env,
) -> IdentityOptions:
    """Apply identity-related environment overrides to a base identity options model."""
    if not env_setting_is_set(env, IDENTITY_ENV_SETTINGS):
        return identity_options

    identity_values = values_from_env_settings(env, IDENTITY_ENV_SETTINGS)
    merged_options = replace(identity_options, **cast(Any, identity_values))
    object.__setattr__(
        merged_options,
        "token_secrets_configured",
        _identity_token_secrets_configured(
            identity_options,
            auth_config,
            identity_values,
        ),
    )
    return merged_options


def _identity_token_secrets_configured(
    identity_options: IdentityOptions,
    auth_config: Mapping[str, Any],
    identity_values: Mapping[str, Any],
) -> bool:
    return _identity_token_secret_configured(
        "reset_password_token_secret",
        identity_options,
        auth_config,
        identity_values,
    ) and _identity_token_secret_configured(
        "verification_token_secret",
        identity_options,
        auth_config,
        identity_values,
    )


def _identity_token_secret_configured(
    field_name: str,
    identity_options: IdentityOptions,
    auth_config: Mapping[str, Any],
    identity_values: Mapping[str, Any],
) -> bool:
    if field_name in identity_values:
        return _identity_token_secret_value_configured(identity_values[field_name])
    if field_name in auth_config:
        return _identity_token_secret_value_configured(auth_config[field_name])

    return identity_options.token_secrets_configured


def _identity_token_secret_value_configured(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not is_generate_local_identity_secret(value)
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
    env: Mapping[str, str | None] | Env,
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


def _configured_env_value(
    env: Mapping[str, str | None] | Env, field_name: str
) -> str | None:
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
