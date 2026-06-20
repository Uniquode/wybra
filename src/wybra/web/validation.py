from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wybra.core.composition import CompositionError
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check
from wybra.web.routes import load_module_routes, route_prefixes_from_settings
from wybra.web.routes.discovery import discover_module_surfaces

if TYPE_CHECKING:
    from wybra.core.composition import AppConfig


class WebValidationSettings(Protocol):
    """Settings shape required by reusable web validation."""

    project_root: Path
    static_url_path: str
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...


def validate_web(settings: WebValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    record_check(
        checks,
        errors,
        passed=bool(settings.static_url_path.strip()),
        description=f"static URL path is configured: {settings.static_url_path}",
        error="Static URL path must not be empty.",
    )

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
            description="configured module surfaces load",
            error=f"Configured module surface validation failed: {exc}",
        )
        return ValidationResult(name="web", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description=("configured module surfaces load: " + ", ".join(settings.modules)),
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
            description="module routers compose",
            error=f"Module route composition failed: {exc}",
        )
        return ValidationResult(name="web", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description=(
            "module routers compose: "
            + ", ".join(
                f"{router.module_name}.{router.label}" for router in configured_routers
            )
        ),
    )

    return ValidationResult(name="web", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"web": validate_web}

__all__ = (
    "WebValidationSettings",
    "validate_web",
    "validation_targets",
)
