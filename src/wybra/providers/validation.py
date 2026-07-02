from __future__ import annotations

from typing import Protocol

from wybra.core.exceptions import ConfigurationError
from wybra.providers.settings import APPLE_PROVIDER_NAME, ProvidersSettings
from wybra.tools.validation.core import ValidationResult, record_check


class ProvidersValidationSettings(Protocol):
    config: object


def validate_provider_configuration(settings: ProvidersSettings) -> None:
    for provider in settings.enabled_providers:
        if provider.client_id is None:
            raise ConfigurationError(
                f"Provider {provider.name!r} is enabled but client_id is missing."
            )
        if provider.name == APPLE_PROVIDER_NAME:
            if provider.team_id is None:
                raise ConfigurationError(
                    f"Provider {provider.name!r} is enabled but team_id is missing."
                )
            if provider.key_id is None:
                raise ConfigurationError(
                    f"Provider {provider.name!r} is enabled but key_id is missing."
                )
        provider.required_provider_secret_reference()


def validate_providers(settings: ProvidersValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks = []
    try:
        provider_settings = ProvidersSettings.load_settings(settings.config)
        validate_provider_configuration(provider_settings)
    except ConfigurationError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="providers settings are valid",
            error=str(exc),
        )
        return ValidationResult(
            name="providers",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    record_check(
        checks,
        errors,
        passed=True,
        description="providers settings are valid",
    )
    return ValidationResult(
        name="providers", errors=tuple(errors), checks=tuple(checks)
    )


validation_targets = {"providers": validate_providers}


__all__ = (
    "ProvidersValidationSettings",
    "validate_provider_configuration",
    "validate_providers",
    "validation_targets",
)
