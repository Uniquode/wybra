from __future__ import annotations

from typing import Protocol

from wevra.core.resources import PackageResourceSource, first_existing_resource
from wevra.tools.validation.core import ValidationCheck, ValidationResult, record_check
from wevra.widgets.config import (
    THEME_FEATURE,
    WIDGETS_CONFIG_SECTION,
    to_widget_features,
)
from wevra.widgets.features import THEME_WIDGET


class WidgetsValidationSettings(Protocol):
    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_widgets(settings: WidgetsValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    if "wevra.widgets" not in settings.modules:
        record_check(
            checks,
            errors,
            passed=True,
            description="wevra.widgets is not configured",
        )
        return ValidationResult(
            name="widgets",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    record_check(
        checks,
        errors,
        passed=True,
        description="wevra.widgets is configured",
    )

    enabled_features = _enabled_features_from_settings(settings)
    record_check(
        checks,
        errors,
        passed=enabled_features is not None,
        description="widget feature configuration is valid",
        error="Widget feature configuration is invalid.",
    )
    if enabled_features is None:
        return ValidationResult(
            name="widgets",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    if THEME_FEATURE in enabled_features:
        _validate_theme_resources(checks, errors)

    return ValidationResult(name="widgets", errors=tuple(errors), checks=tuple(checks))


def _enabled_features_from_settings(
    settings: WidgetsValidationSettings,
) -> tuple[str, ...] | None:
    app_config = getattr(settings, "app_config", None)
    if app_config is None:
        return (THEME_FEATURE,)
    raw_config = getattr(app_config, "raw_config", {})
    widgets_config = raw_config.get(WIDGETS_CONFIG_SECTION, {})
    try:
        return to_widget_features(widgets_config.get("features", (THEME_FEATURE,)))
    except ValueError:
        return None


def _validate_theme_resources(
    checks: list[ValidationCheck],
    errors: list[str],
) -> None:
    template_source = PackageResourceSource(
        package="wevra.widgets",
        directory="templates",
    )
    static_source = PackageResourceSource(package="wevra.widgets", directory="static")
    for template in THEME_WIDGET.templates:
        record_check(
            checks,
            errors,
            passed=first_existing_resource((template_source,), template) is not None,
            description=f"widget template exists: {template}",
            error=f"Missing widget template: {template}",
        )
    for asset in THEME_WIDGET.static_assets:
        record_check(
            checks,
            errors,
            passed=first_existing_resource((static_source,), asset) is not None,
            description=f"widget static asset exists: {asset}",
            error=f"Missing widget static asset: {asset}",
        )


validation_targets = {"widgets": validate_widgets}

__all__ = (
    "WidgetsValidationSettings",
    "validate_widgets",
    "validation_targets",
)
