"""Validation target for configured security policy."""

from __future__ import annotations

from typing import Protocol

from wybra.config import ConfigService
from wybra.security.capabilities import security_provider_configured
from wybra.security.settings import SecuritySettings
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class SecurityValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...

    config: ConfigService


def validate_security(settings: SecurityValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    if not security_provider_configured(settings.modules):
        record_check(
            checks,
            errors,
            passed=True,
            description="security module is not configured",
        )
        return ValidationResult(
            name="security",
            errors=tuple(errors),
            checks=tuple(checks),
        )
    if "wybra.security" not in settings.modules:
        record_check(
            checks,
            errors,
            passed=True,
            description="replacement security capability provider is configured",
        )
        return ValidationResult(
            name="security",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    try:
        security_settings = SecuritySettings.load_settings(settings.config)
    except Exception as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="security settings load",
            error=f"Security settings failed to load: {exc}",
        )
    else:
        record_check(
            checks,
            errors,
            passed=True,
            description=(
                "security settings load: "
                f"coop={security_settings.cross_origin_opener_policy!r}, "
                f"asset_cors_enabled={security_settings.asset_cors.enabled}"
            ),
        )

    return ValidationResult(name="security", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"security": validate_security}
