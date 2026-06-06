from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wevra.core.composition import CompositionError
from wevra.core.resources import PackageResourceSource, first_existing_resource
from wevra.tools.validation.core import ValidationCheck, ValidationResult, record_check
from wevra.web.context import validate_context_providers
from wevra.web.rendering import TemplateRenderer
from wevra.web.routes import load_module_routes
from wevra.web.routes.discovery import (
    discover_module_surfaces,
    static_sources_from_modules,
    template_sources_from_modules,
)
from wevra.web.style_contract import (
    REQUIRED_STATIC_ASSETS,
    REQUIRED_THEME_SELECTORS,
    REQUIRED_THEME_TOKENS,
)

if TYPE_CHECKING:
    from wevra.core.composition import AppConfig

ResourceForValidation = Traversable | Path


class WebValidationSettings(Protocol):
    """Settings shape required by reusable web validation.

    This extends the route-composition shape with optional filesystem override
    roots and renderer policy. Concrete applications may expose these fields
    through a dataclass, settings object, or test double.
    """

    template_root: Path | None
    static_root: Path | None
    static_url_path: str
    template_auto_reload: bool | None
    template_cache_size: int
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...

    @property
    def uses_filesystem_template_root(self) -> bool: ...

    @property
    def uses_filesystem_static_root(self) -> bool: ...


class PostFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.contains_post_form = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "form":
            return

        for name, value in attrs:
            if name.lower() == "method" and value is not None:
                if value.strip().lower() == "post":
                    self.contains_post_form = True
                return


def validate_web(settings: WebValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    if _uses_filesystem_template_root(settings):
        template_root = settings.template_root
        record_check(
            checks,
            errors,
            passed=template_root is not None and template_root.is_dir(),
            description=f"template root exists: {template_root}",
            error=f"Missing template root: {template_root}",
        )

    if _uses_filesystem_static_root(settings):
        static_root = settings.static_root
        record_check(
            checks,
            errors,
            passed=static_root is not None and static_root.is_dir(),
            description=f"static root exists: {static_root}",
            error=f"Missing static root: {static_root}",
        )

    record_check(
        checks,
        errors,
        passed=bool(settings.static_url_path.strip()),
        description=f"static URL path is configured: {settings.static_url_path}",
        error="Static URL path must not be empty.",
    )

    try:
        module_surfaces = discover_module_surfaces(
            settings.modules,
            include_routes=True,
            include_context=True,
        )
        validate_context_providers(
            provider
            for surface in module_surfaces
            for provider in surface.context_providers
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
    record_check(
        checks,
        errors,
        passed=True,
        description="template context providers validate",
    )

    try:
        route_set = load_module_routes(
            settings.modules,
            route_prefixes=_route_prefixes(settings),
        )
    except CompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="module routes compose",
            error=f"Module route composition failed: {exc}",
        )
        return ValidationResult(name="web", errors=tuple(errors), checks=tuple(checks))

    record_check(
        checks,
        errors,
        passed=True,
        description="module routes compose",
    )
    template_sources = _template_sources(settings)
    renderer: TemplateRenderer | None = None

    route_definitions = tuple(route_set.page_routes) + tuple(route_set.partial_routes)
    for definition in route_definitions:
        template_name = getattr(definition.view, "template_name", None)
        if template_name is None:
            continue

        template_resource = _template_resource(
            settings,
            template_sources,
            template_name,
        )
        if not record_check(
            checks,
            errors,
            passed=template_resource is not None,
            description=f"route template exists: {definition.name} -> {template_name}",
            error=f"Missing template: {_template_location(settings, template_name)}",
        ):
            continue

        assert template_resource is not None
        template_content = _read_template_content(
            template_resource,
            checks,
            errors,
            description=f"template reads as UTF-8: {template_name}",
        )
        if template_content is None:
            continue

        if _contains_post_form(template_content):
            record_check(
                checks,
                errors,
                passed='name="{{ csrf_field_name }}"' in template_content,
                description=f"POST form CSRF field exists: {template_name}",
                error=(
                    "POST form template must include CSRF field: "
                    f"{_template_location(settings, template_name)}"
                ),
            )

        try:
            if renderer is None:
                renderer = _template_renderer(settings, template_sources)
            renderer.environment.get_template(template_name)
        except Exception as exc:  # pragma: no cover - defensive guard
            record_check(
                checks,
                errors,
                passed=False,
                description=f"template loads: {template_name}",
                error=f"Template load failed for {template_name}: {exc}",
            )
        else:
            record_check(
                checks,
                errors,
                passed=True,
                description=f"template loads: {template_name}",
            )

    static_sources = _static_sources(settings)
    for asset in REQUIRED_STATIC_ASSETS:
        asset_resource = _static_resource(settings, static_sources, asset)
        if not record_check(
            checks,
            errors,
            passed=asset_resource is not None,
            description=f"static asset exists: {asset}",
            error=f"Missing static asset: {_static_location(settings, asset)}",
        ):
            continue

        if asset != "styles/app.css":
            continue

        assert asset_resource is not None
        stylesheet_content = _read_resource_content(
            asset_resource,
            checks,
            errors,
            description=f"static asset reads as UTF-8: {asset}",
        )
        if stylesheet_content is None:
            continue

        for token in REQUIRED_THEME_TOKENS:
            record_check(
                checks,
                errors,
                passed=token in stylesheet_content,
                description=f"theme token present: {token}",
                error=f"Missing theme token: {token}",
            )

        for selector in REQUIRED_THEME_SELECTORS:
            record_check(
                checks,
                errors,
                passed=selector in stylesheet_content,
                description=f"theme selector present: {selector}",
                error=f"Missing theme selector: {selector}",
            )

    return ValidationResult(name="web", errors=tuple(errors), checks=tuple(checks))


def _contains_post_form(template_content: str) -> bool:
    parser = PostFormParser()
    parser.feed(template_content)
    parser.close()
    return parser.contains_post_form


def _template_sources(
    settings: WebValidationSettings,
) -> tuple[PackageResourceSource, ...]:
    return template_sources_from_modules(settings.modules)


def _template_renderer(
    settings: WebValidationSettings,
    template_sources: tuple[PackageResourceSource, ...],
) -> TemplateRenderer:
    return TemplateRenderer(
        template_root=(
            settings.template_root if _uses_filesystem_template_root(settings) else None
        ),
        template_sources=template_sources,
        auto_reload=settings.template_auto_reload,
        cache_size=settings.template_cache_size,
    )


def _template_resource(
    settings: WebValidationSettings,
    template_sources: tuple[PackageResourceSource, ...],
    template_name: str,
) -> ResourceForValidation | None:
    if _uses_filesystem_template_root(settings) and settings.template_root is not None:
        template_path = settings.template_root / template_name
        if template_path.is_file():
            return template_path

    return first_existing_resource(template_sources, template_name)


def _static_sources(
    settings: WebValidationSettings,
) -> tuple[PackageResourceSource, ...]:
    if _uses_filesystem_static_root(settings):
        return ()

    return static_sources_from_modules(settings.modules)


def _static_resource(
    settings: WebValidationSettings,
    static_sources: tuple[PackageResourceSource, ...],
    asset: str,
) -> ResourceForValidation | None:
    if static_sources:
        return first_existing_resource(static_sources, asset)
    if not _uses_filesystem_static_root(settings):
        return None

    if settings.static_root is None:
        return None

    asset_path = settings.static_root / asset
    return asset_path if asset_path.is_file() else None


def _read_template_content(
    template_resource: ResourceForValidation,
    checks: list[ValidationCheck],
    errors: list[str],
    *,
    description: str,
) -> str | None:
    return _read_resource_content(
        template_resource,
        checks,
        errors,
        description=description,
    )


def _read_resource_content(
    resource: ResourceForValidation,
    checks: list[ValidationCheck],
    errors: list[str],
    *,
    description: str,
) -> str | None:
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description=description,
            error=f"Unable to read {resource}: {exc}",
        )
        return None


def _template_location(settings: WebValidationSettings, template_name: str) -> str:
    if _uses_filesystem_template_root(settings) and settings.template_root is not None:
        return str(settings.template_root / template_name)

    return template_name


def _static_location(settings: WebValidationSettings, asset: str) -> str:
    if _uses_filesystem_static_root(settings) and settings.static_root is not None:
        return str(settings.static_root / asset)

    return asset


def _uses_filesystem_template_root(settings: WebValidationSettings) -> bool:
    return settings.uses_filesystem_template_root


def _uses_filesystem_static_root(settings: WebValidationSettings) -> bool:
    return settings.uses_filesystem_static_root


def _route_prefixes(settings: WebValidationSettings) -> Mapping[str, str]:
    app_config = settings.app_config
    if app_config is None:
        return {}

    prefixes = app_config.routes.prefixes
    if isinstance(prefixes, Mapping):
        return prefixes

    return {}


validation_targets = {"web": validate_web}

__all__ = (
    "PostFormParser",
    "WebValidationSettings",
    "_contains_post_form",
    "validate_web",
    "validation_targets",
)
