from __future__ import annotations

from typing import Protocol

from wybra.config import ConfigService
from wybra.forms.capabilities import forms_provider_configured
from wybra.forms.settings import FormsSettings
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class FormsValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...

    config: ConfigService


def validate_forms(settings: FormsValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    if not forms_provider_configured(settings.modules):
        record_check(
            checks,
            errors,
            passed=True,
            description="forms module is not configured",
        )
        return ValidationResult(
            name="forms",
            errors=tuple(errors),
            checks=tuple(checks),
        )
    if "wybra.forms" not in settings.modules:
        record_check(
            checks,
            errors,
            passed=True,
            description="replacement forms capability provider is configured",
        )
        return ValidationResult(
            name="forms",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    try:
        forms_settings = FormsSettings.load_settings(settings.config)
    except Exception as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="forms settings load",
            error=f"Forms settings failed to load: {exc}",
        )
    else:
        record_check(
            checks,
            errors,
            passed=True,
            description=(
                "forms settings load: "
                f"csrf_cookie_secure={forms_settings.cookie_secure}"
            ),
        )

    return ValidationResult(name="forms", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"forms": validate_forms}


__all__ = ("FormsValidationSettings", "validate_forms", "validation_targets")
