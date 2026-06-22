from __future__ import annotations

from typing import Protocol

from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class ErrorsValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_errors(settings: ErrorsValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    record_check(
        checks,
        errors,
        passed=True,
        description=(
            "errors module is configured"
            if "wybra.errors" in settings.modules
            else "errors module is not configured"
        ),
    )
    return ValidationResult(name="errors", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"errors": validate_errors}
