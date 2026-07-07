from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, cast

from envex import Env
from starlette.datastructures import State

from wybra.auth.options import (
    PASSKEY,
    PROVIDER,
    TOTP,
    TOTP_MODE,
    IdentityIntegration,
    IdentityOptions,
    identity_env_setting_name,
    is_generate_local_identity_secret,
)
from wybra.config import BaseSettings, ConfigDef, ConfigField, ConfigGroup, to_bool
from wybra.config.service import ConfigService
from wybra.config.sources import AppConfigSource
from wybra.core.composition import AppConfig
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.exceptions import ConfigurationError
from wybra.core.runtime import (
    LOCAL_ENVIRONMENT,
    DeploymentEnvironment,
    normalise_deployment_environment,
)
from wybra.core.settings import (
    EnvironmentSetting,
    env_setting_is_set,
    values_from_env_settings,
)
from wybra.db.urls import resolve_database_url

DATABASE_URL_ENV = "DATABASE_URL"
AUTH_SETTINGS_OWNER: Final = "wybra.auth"


APP_CONFIG_SECTION: Final = "app"
AUTH_CONFIG_SECTION: Final = "auth"
PASSWORD_SECTION_FIELD = "password"
PASSWORD_POLICY_SECTION_FIELD = "policy"
PASSKEY_SECTION_FIELD = "passkeys"
PASSWORD_POLICY_CONFIG_SECTION: Final = (
    f"{AUTH_CONFIG_SECTION}.{PASSWORD_SECTION_FIELD}.{PASSWORD_POLICY_SECTION_FIELD}"
)
PASSKEY_CONFIG_SECTION: Final = f"{AUTH_CONFIG_SECTION}.{PASSKEY_SECTION_FIELD}"
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
PASSKEY_OPTION_MAP = {
    "rp_id": "passkey_rp_id",
    "rp_name": "passkey_rp_name",
    "allowed_origins": "passkey_allowed_origins",
    "timeout_seconds": "passkey_timeout_seconds",
    "user_verification": "passkey_user_verification",
    "user_verification_satisfies_totp": "passkey_user_verification_satisfies_totp",
    "attestation": "passkey_attestation",
    "discoverable_credentials": "passkey_discoverable_credentials",
    "counter_policy": "passkey_counter_policy",
}
PASSWORD_OPTION_FIELDS = frozenset({PASSWORD_POLICY_SECTION_FIELD})
AUTH_OPTION_FIELDS = IDENTITY_OPTION_FIELDS | {
    PASSKEY_SECTION_FIELD,
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


def _to_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"must be an integer value (got {value!r}).") from exc
    raise ValueError(f"must be an integer value (got {value!r}).")


def _to_positive_int(value: object) -> int:
    int_value = _to_int(value)
    if int_value <= 0:
        raise ValueError(f"must be a positive integer value (got {int_value!r}).")
    return int_value


module_config: Final = ConfigDef(
    {
        APP_CONFIG_SECTION: ConfigGroup(
            fields=(ConfigField(name="database_url", env=DATABASE_URL_ENV),),
        ),
        AUTH_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="account_creation_policy",
                    env=ENV_ACCOUNT_CREATION_POLICY,
                ),
                ConfigField(name="provider_enabled", transform=to_bool),
                ConfigField(name="passkey_enabled", transform=to_bool),
                ConfigField(name=TOTP_MODE, env=ENV_TOTP_MODE),
                ConfigField(name="session_cookie_name", env=ENV_SESSION_COOKIE),
                ConfigField(
                    name="session_cookie_force_secure",
                    env=ENV_SESSION_FORCE_SECURE,
                    transform=to_bool,
                ),
                ConfigField(
                    name="session_lifetime_seconds",
                    env=ENV_SESSION_LIFETIME,
                    transform=_to_positive_int,
                ),
                ConfigField(
                    name="reset_password_token_secret",
                    env=ENV_RESET_SECRET,
                ),
                ConfigField(
                    name="verification_token_secret",
                    env=ENV_VERIFICATION_SECRET,
                ),
                ConfigField(
                    name="totp_allowed_drift",
                    env=ENV_TOTP_ALLOWED_DRIFT,
                    transform=_to_int,
                ),
                ConfigField(
                    name="totp_period_seconds",
                    env=ENV_TOTP_PERIOD_SECONDS,
                    transform=_to_positive_int,
                ),
                ConfigField(
                    name="totp_challenge_expiry_seconds",
                    env=ENV_TOTP_CHALLENGE_EXPIRY_SECONDS,
                    transform=_to_positive_int,
                ),
                ConfigField(
                    name="totp_recovery_window_seconds",
                    env=ENV_TOTP_RECOVERY_WINDOW_SECONDS,
                    transform=_to_positive_int,
                ),
            ),
        ),
        PASSWORD_POLICY_CONFIG_SECTION: ConfigGroup(
            fields=tuple(
                ConfigField(name=field_name)
                for field_name in PASSWORD_POLICY_OPTION_MAP
            ),
        ),
        PASSKEY_CONFIG_SECTION: ConfigGroup(
            fields=tuple(
                ConfigField(name=field_name) for field_name in PASSKEY_OPTION_MAP
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class AuthSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = AUTH_CONFIG_SECTION

    database_url: str
    identity_options: IdentityOptions = field(default_factory=IdentityOptions)
    deployment_environment: DeploymentEnvironment = LOCAL_ENVIRONMENT

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Mapping[str, Any]],
        *,
        app_config: AppConfig,
        environ: Mapping[str, str] | None = None,
        deployment_environment: DeploymentEnvironment | str | None = LOCAL_ENVIRONMENT,
    ) -> AuthSettings:  # ty: ignore[invalid-method-override]
        """Load auth settings from ConfigService or section-keyed raw config.

        Plain mappings are expected to use ``{section_name: {field: value}}``
        shape so auth settings can combine ``app``, ``auth``, and nested auth
        policy sections consistently.
        """
        env_values = os.environ if environ is None else environ
        env = Env(
            environ=dict(env_values),
            readenv=False,
            update=False,
        )
        if app_config.auth is not None and not isinstance(app_config.auth, Mapping):
            raise ConfigurationError(
                "Invalid auth configuration: [auth] must be a table when defined."
            )
        app_auth_config = app_config.auth or {}
        auth_config = _merge_auth_with_loaded_precedence(
            app_auth_config,
            cls.section_values(config, AUTH_CONFIG_SECTION),
        )
        _reject_unknown_auth_options(auth_config)
        database_url = _configured_database_url(config, app_config, env)
        identity_values = _identity_values_from_environment(env)
        identity_options = _identity_options_from_config(
            config,
            {**auth_config, **identity_values},
        )
        if identity_values:
            object.__setattr__(
                identity_options,
                "token_secrets_configured",
                _identity_token_secrets_configured(
                    identity_options,
                    auth_config,
                    identity_values,
                ),
            )
        return cls(
            database_url=resolve_database_url(
                database_url,
                app_config.config_path.resolve().parent,
            ),
            identity_options=identity_options,
            deployment_environment=normalise_deployment_environment(
                deployment_environment
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deployment_environment",
            normalise_deployment_environment(self.deployment_environment),
        )

    @property
    def owner(self) -> str:
        return AUTH_SETTINGS_OWNER

    def integration_enabled(self, integration: IdentityIntegration) -> bool:
        return self.identity_options.integration_enabled(integration)

    def is_totp_enabled(self) -> bool:
        return self.integration_enabled(cast(IdentityIntegration, TOTP))

    def is_local(self) -> bool:
        return self.deployment_environment == LOCAL_ENVIRONMENT


def auth_settings_from_state(state: State) -> AuthSettings:
    settings = getattr(state, "auth_settings", None)
    if not isinstance(settings, AuthSettings):
        raise RuntimeError("Auth settings are not configured on the application.")

    return settings


def identity_options_from_state(state: State) -> IdentityOptions:
    return auth_settings_from_state(state).identity_options


def validate_auth_settings(
    settings: AuthSettings,
) -> None:
    if settings.is_local():
        return

    identity_options = settings.identity_options
    if (
        is_generate_local_identity_secret(identity_options.reset_password_token_secret)
        or is_generate_local_identity_secret(identity_options.verification_token_secret)
        or not identity_options.token_secrets_configured
    ):
        raise ConfigurationError(
            "Non-local deployments must configure identity reset and "
            "verification token secrets."
        )
    if not identity_options.session_cookie_force_secure:
        raise ConfigurationError(
            "Non-local deployments must force secure session cookies; set "
            "SESSION_FORCE_SECURE=true or auth.session_cookie_force_secure = true."
        )


def load_runtime_auth_settings(
    *,
    app_config: AppConfig | None,
    deployment_environment: DeploymentEnvironment | str | None,
    database_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AuthSettings:
    """Load and validate auth settings for application runtime composition."""

    if app_config is not None:
        config = ConfigService(
            [AppConfigSource(app_config)],
            config_defs=(RUNTIME_CONFIG_DEF, AuthSettings.module_config),
            discover_module_config=False,
        )
        settings = AuthSettings.load_settings(
            config,
            app_config=app_config,
            environ=environ,
            deployment_environment=deployment_environment,
        )
    else:
        if database_url is None or not database_url.strip():
            raise ConfigurationError(
                "Database URL is required when app config is not available."
            )
        settings = AuthSettings(
            database_url=database_url,
            deployment_environment=normalise_deployment_environment(
                deployment_environment
            ),
        )

    validate_auth_settings(settings)
    return settings


def supported_auth_environment_names() -> tuple[str, ...]:
    """Return auth-owned identity environment variable names.

    ``DATABASE_URL`` is intentionally excluded. Database connection policy is a
    persistence concern validated by ``wybra.db`` and may be supplied by app
    config, CLI override, or runtime composition rather than auth settings.
    """

    return tuple(setting.name for setting in IDENTITY_ENV_SETTINGS)


def _identity_values_from_environment(env: Env) -> dict[str, Any]:
    if not env_setting_is_set(env, IDENTITY_ENV_SETTINGS):
        return {}

    return values_from_env_settings(env, IDENTITY_ENV_SETTINGS)


def _merge_auth_with_loaded_precedence(
    app_config_auth: Mapping[str, Any],
    loaded_auth_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge app auth config with loaded config.

    Loaded config wins for top-level keys. When both sources define
    ``auth.password`` as mappings, their nested values are merged with loaded
    config precedence. Shape mismatches fail fast instead of silently replacing
    malformed password config.
    """
    merged = dict(app_config_auth)
    for key, value in loaded_auth_config.items():
        if key in {PASSWORD_SECTION_FIELD, PASSKEY_SECTION_FIELD}:
            current_value = merged.get(key)
            loaded_is_mapping = isinstance(value, Mapping)
            current_is_mapping = isinstance(current_value, Mapping)
            if loaded_is_mapping and current_is_mapping:
                merged[key] = _merge_nested_auth_table_with_loaded_precedence(
                    cast(Mapping[str, Any], current_value),
                    value,
                )
                continue
            if current_value is not None and loaded_is_mapping != current_is_mapping:
                raise ConfigurationError(
                    f"Conflicting auth.{key} configuration: app config and "
                    "loaded config must both be tables when both are defined."
                )
            merged[key] = value
            continue
        merged[key] = value
    return merged


def _merge_nested_auth_table_with_loaded_precedence(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge nested auth config; loaded values win."""
    merged = dict(base)
    for key, value in override.items():
        if (
            key == PASSWORD_POLICY_SECTION_FIELD
            and isinstance(value, Mapping)
            and isinstance(merged.get(key), Mapping)
        ):
            merged[key] = {**cast(Mapping[str, Any], merged[key]), **value}
            continue
        merged[key] = value
    return merged


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
        _reject_unknown_passkey_options(auth_config)
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


def _reject_unknown_passkey_options(auth_config: Mapping[str, Any]) -> None:
    passkey_config = auth_config.get(PASSKEY_SECTION_FIELD)
    if passkey_config is None:
        return

    if not isinstance(passkey_config, dict):
        raise ConfigurationError(
            f"Auth passkey config must be an [auth.{PASSKEY_SECTION_FIELD}] table."
        )

    unknown_fields = sorted(set(passkey_config) - set(PASSKEY_OPTION_MAP))
    if unknown_fields:
        unknown_list = ", ".join(unknown_fields)
        allowed_fields = ", ".join(sorted(PASSKEY_OPTION_MAP))
        raise ConfigurationError(
            f"Unknown option(s) in [auth.{PASSKEY_SECTION_FIELD}] configuration: "
            f"{unknown_list}. Allowed options are: {allowed_fields}."
        )


def _configured_database_url(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    app_config: AppConfig,
    env: Mapping[str, str | None] | Env,
) -> str:
    app_values = AuthSettings.section_values(config, APP_CONFIG_SECTION)
    database_url = (
        _configured_env_value(env, DATABASE_URL_ENV)
        or app_values.get("database_url")
        or app_config.database_url
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


def _identity_options_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    auth_config: Mapping[str, Any],
) -> IdentityOptions:
    identity_kwargs = {
        key: value
        for key, value in auth_config.items()
        if key in IDENTITY_OPTION_FIELDS
    }
    identity_kwargs.update(_passkey_options_from_config(config, auth_config))
    identity_kwargs.update(_password_policy_options_from_config(config, auth_config))
    return IdentityOptions(**identity_kwargs)


def _passkey_options_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    auth_config: Mapping[str, Any],
) -> dict[str, Any]:
    passkey_config = auth_config.get(PASSKEY_SECTION_FIELD, {})
    if PASSKEY_SECTION_FIELD in auth_config and not isinstance(passkey_config, dict):
        raise ConfigurationError(
            f"Auth passkey config must be an [auth.{PASSKEY_SECTION_FIELD}] table."
        )
    if not isinstance(passkey_config, dict):
        passkey_config = {}
    if not passkey_config:
        passkey_config = AuthSettings.section_values(config, PASSKEY_CONFIG_SECTION)

    return {
        identity_option: passkey_config[config_key]
        for config_key, identity_option in PASSKEY_OPTION_MAP.items()
        if config_key in passkey_config
    }


def _password_policy_options_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    auth_config: Mapping[str, Any],
) -> dict[str, Any]:
    password_config = auth_config.get(PASSWORD_SECTION_FIELD, {})
    if PASSWORD_SECTION_FIELD in auth_config and not isinstance(password_config, dict):
        raise ConfigurationError(
            f"Auth password config must be an [auth.{PASSWORD_SECTION_FIELD}] table."
        )
    if not isinstance(password_config, dict):
        password_config = {}

    if PASSWORD_POLICY_SECTION_FIELD in password_config:
        policy_config = password_config[PASSWORD_POLICY_SECTION_FIELD]
        if not isinstance(policy_config, dict):
            raise ConfigurationError(
                "Auth password policy config must be an "
                f"[auth.{PASSWORD_SECTION_FIELD}.{PASSWORD_POLICY_SECTION_FIELD}] "
                "table."
            )
    else:
        policy_config = AuthSettings.section_values(
            config,
            PASSWORD_POLICY_CONFIG_SECTION,
        )

    return {
        identity_option: policy_config[config_key]
        for config_key, identity_option in PASSWORD_POLICY_OPTION_MAP.items()
        if config_key in policy_config
    }
