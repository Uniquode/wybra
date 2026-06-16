from __future__ import annotations

from typing import Protocol

from wevra.profile.models import metadata
from wevra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class ProfileValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_profile(settings: ProfileValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    record_check(
        checks,
        errors,
        passed="profile_user_profile" in metadata.tables,
        description="profile model metadata exposes profile_user_profile",
        error="Profile metadata must expose profile_user_profile.",
    )
    record_check(
        checks,
        errors,
        passed="wevra.profile" in settings.modules,
        description="profile module is configured",
        error="wevra.profile must be configured to validate profile resources.",
    )
    return ValidationResult(name="profile", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"profile": validate_profile}

__all__ = ("ProfileValidationSettings", "validate_profile", "validation_targets")
