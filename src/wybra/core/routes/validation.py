from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from wybra.core.composition import CompositionError
from wybra.core.routes.discovery import discover_module_surfaces
from wybra.core.routes.registration import (
    load_module_routes,
    route_prefixes_from_settings,
)
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check

if TYPE_CHECKING:
    from wybra.core.composition import AppConfig


class RouteValidationSettings(Protocol):
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_routes(settings: RouteValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    try:
        discover_module_surfaces(
            settings.modules,
            include_routes=True,
        )
    except CompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="configured route modules load",
            error=f"Configured route module validation failed: {exc}",
        )
        return ValidationResult(
            name="routes",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    record_check(
        checks,
        errors,
        passed=True,
        description=("configured route modules load: " + ", ".join(settings.modules)),
    )

    try:
        configured_routers = load_module_routes(
            settings.modules,
            route_prefixes=route_prefixes_from_settings(settings),
        )
    except CompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="configured route modules compose",
            error=f"Configured route module composition failed: {exc}",
        )
        return ValidationResult(
            name="routes",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    record_check(
        checks,
        errors,
        passed=True,
        description=(
            "configured route modules compose: "
            + ", ".join(
                f"{router.module_name}.{router.label}" for router in configured_routers
            )
        ),
    )

    return ValidationResult(name="routes", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"routes": validate_routes}


__all__ = (
    "RouteValidationSettings",
    "validate_routes",
    "validation_targets",
)
