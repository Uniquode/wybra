"""Validation checks for API response behaviour."""

from __future__ import annotations

from typing import Protocol

from wybra.api.capabilities import api_provider_configured
from wybra.api.config import parse_api_link_mode
from wybra.api.settings import ApiSettings
from wybra.config import ConfigService
from wybra.config.transforms import to_url_path
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class ApiValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...

    config: ConfigService


def validate_api(settings: ApiValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    if not api_provider_configured(settings.modules):
        record_check(
            checks,
            errors,
            passed=True,
            description="api module is not configured",
        )
        return ValidationResult(name="api", errors=tuple(errors), checks=tuple(checks))
    if "wybra.api" not in settings.modules:
        record_check(
            checks,
            errors,
            passed=True,
            description="replacement API capability provider is configured",
        )
        return ValidationResult(name="api", errors=tuple(errors), checks=tuple(checks))

    try:
        api_settings = ApiSettings.load_settings(settings.config)
    except Exception as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="API settings load",
            error=f"API settings failed to load: {exc}",
        )
        return ValidationResult(name="api", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description=(
            "API settings load: "
            f"path_prefix={api_settings.path_prefix!r}, "
            f"paging_link_mode={api_settings.paging_link_mode.value!r}"
        ),
    )

    record_check(
        checks,
        errors,
        passed=_valid_path_prefix(api_settings.path_prefix),
        description="API path prefix is configured",
        error="API path prefix must be non-empty and not root-mounted.",
    )
    record_check(
        checks,
        errors,
        passed=_valid_link_mode(api_settings.paging_link_mode),
        description="API paging link mode is configured",
        error="API paging link mode must be supported.",
    )

    return ValidationResult(name="api", errors=tuple(errors), checks=tuple(checks))


def _valid_path_prefix(value: object) -> bool:
    try:
        return to_url_path(value, name="app.api.path_prefix") != "/"
    except ValueError:
        return False


def _valid_link_mode(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_api_link_mode(value)
    except ValueError:
        return False
    return True


validation_targets = {"api": validate_api}


__all__ = ("ApiValidationSettings", "validate_api", "validation_targets")
