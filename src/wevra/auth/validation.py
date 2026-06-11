from __future__ import annotations

from typing import Protocol

from wevra.auth.configuration import ConfigurationError
from wevra.auth.settings import (
    DATABASE_URL_ENV,
    AuthSettings,
    load_auth_settings_from_config,
    validate_auth_settings,
)
from wevra.config import AppConfigSource, ConfigService
from wevra.core.composition import AppConfig
from wevra.tools.validation.core import ValidationResult, record_check


class AuthValidationSettings(Protocol):
    database_url: str
    app_config: AppConfig | None


def validate_auth(settings: AuthValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks = []

    try:
        auth_settings = _load_auth_settings(settings)
        validate_auth_settings(
            auth_settings,
            allow_local_secrets=_allow_local_auth_secrets(settings),
        )
    except ConfigurationError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="auth settings are valid for the current environment",
            error=str(exc),
        )
        return ValidationResult(name="auth", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description="auth settings are valid for the current environment",
    )
    return ValidationResult(name="auth", errors=tuple(errors), checks=tuple(checks))


def _load_auth_settings(settings: AuthValidationSettings) -> AuthSettings:
    if settings.app_config is None:
        return AuthSettings(database_url=settings.database_url)

    return load_auth_settings_from_config(
        ConfigService([AppConfigSource(settings.app_config)]),
        app_config=settings.app_config,
        environ=_auth_settings_environ(settings),
    )


def _auth_settings_environ(settings: AuthValidationSettings) -> dict[str, str]:
    if not settings.database_url.strip():
        return {}

    return {DATABASE_URL_ENV: settings.database_url}


def _allow_local_auth_secrets(settings: AuthValidationSettings) -> bool:
    return getattr(settings, "deployment_environment", "local") == "local"


validation_targets = {"auth": validate_auth}
