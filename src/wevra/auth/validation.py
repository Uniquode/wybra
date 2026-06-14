from __future__ import annotations

from collections import Counter
from typing import Protocol

from wevra.auth.settings import (
    DATABASE_URL_ENV,
    DeploymentEnvironment,
    load_runtime_auth_settings,
    supported_auth_environment_names,
)
from wevra.core.composition import AppConfig
from wevra.core.exceptions import ConfigurationError
from wevra.core.runtime import (
    DEFAULT_DEPLOYMENT_ENVIRONMENT,
    normalise_deployment_environment,
)
from wevra.tools.validation.core import ValidationCheck, ValidationResult, record_check

AUTH_SETTINGS_VALIDATION_DESCRIPTION = (
    "auth settings are valid for the current environment"
)


class AuthValidationSettings(Protocol):
    database_url: str | None
    app_config: AppConfig | None


def validate_auth(settings: AuthValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    try:
        if settings.app_config is None:
            load_runtime_auth_settings(
                app_config=None,
                database_url=settings.database_url,
                deployment_environment=_deployment_environment(settings),
                environ=_auth_settings_environ(settings),
            )
        else:
            load_runtime_auth_settings(
                app_config=settings.app_config,
                deployment_environment=_deployment_environment(settings),
                environ=_auth_settings_environ(settings),
            )
    except ConfigurationError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description=AUTH_SETTINGS_VALIDATION_DESCRIPTION,
            error=str(exc),
        )
        return ValidationResult(name="auth", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description=AUTH_SETTINGS_VALIDATION_DESCRIPTION,
    )
    auth_environment_names = supported_auth_environment_names()
    counts = Counter(auth_environment_names)
    duplicate_auth_environment_names = sorted(
        name for name, count in counts.items() if count > 1
    )
    uniqueness_description = "auth environment variable names are unique"
    uniqueness_error = "Auth environment variable names must be unique."
    if duplicate_auth_environment_names:
        duplicate_description = ", ".join(duplicate_auth_environment_names)
        uniqueness_description = (
            f"{uniqueness_description} (duplicates: {duplicate_description})"
        )
        uniqueness_error = f"{uniqueness_error} Duplicates: {duplicate_description}"

    record_check(
        checks,
        errors,
        passed=not duplicate_auth_environment_names,
        description=uniqueness_description,
        error=uniqueness_error,
    )
    return ValidationResult(name="auth", errors=tuple(errors), checks=tuple(checks))


def _auth_settings_environ(settings: AuthValidationSettings) -> dict[str, str]:
    if settings.database_url is None or not settings.database_url.strip():
        return {}

    return {DATABASE_URL_ENV: settings.database_url}


def _deployment_environment(
    settings: AuthValidationSettings,
) -> DeploymentEnvironment | str:
    if settings.app_config is None:
        return DEFAULT_DEPLOYMENT_ENVIRONMENT

    return normalise_deployment_environment(settings.app_config.deployment_environment)


validation_targets = {"auth": validate_auth}
