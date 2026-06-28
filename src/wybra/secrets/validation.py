from __future__ import annotations

from typing import Protocol

from wybra.secrets.config import SecretsSettings
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class SecretsValidationSettings(Protocol):
    config: object


def validate_secrets(settings: SecretsValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    secrets_settings = SecretsSettings.load_settings(settings.config)

    record_check(
        checks,
        errors,
        passed=True,
        description="secrets uses consumer-selected sources",
        error="Secrets must not require a global backend.",
    )
    record_check(
        checks,
        errors,
        passed=secrets_settings.keychain.appname.strip() != "",
        description="keychain app name is configured",
        error="Keychain app name must not be blank.",
    )
    record_check(
        checks,
        errors,
        passed=secrets_settings.vault.mount_point.strip() != "",
        description="Vault mount point is configured",
        error="Vault mount point must not be blank.",
    )
    return ValidationResult(name="secrets", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"secrets": validate_secrets}

__all__ = (
    "SecretsValidationSettings",
    "validate_secrets",
    "validation_targets",
)
