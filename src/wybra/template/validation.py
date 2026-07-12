from __future__ import annotations

from html.parser import HTMLParser
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Protocol

from wybra.core.composition import CompositionError
from wybra.core.resources import PackageResourceSource, first_existing_resource
from wybra.template.capabilities import DefaultTemplateCapability
from wybra.template.context import validate_context_providers
from wybra.template.discovery import (
    context_providers_from_modules,
    template_sources_from_modules,
)
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check

ResourceForValidation = Traversable | Path


class TemplateValidationSettings(Protocol):
    project_root: Path
    template_root: Path | None
    template_auto_reload: bool | None
    template_cache_size: int

    @property
    def modules(self) -> tuple[str, ...]: ...

    @property
    def uses_filesystem_template_root(self) -> bool: ...


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


def validate_template(settings: TemplateValidationSettings) -> ValidationResult:
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

    try:
        validate_context_providers(context_providers_from_modules(settings.modules))
    except CompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="template context providers validate",
            error=f"Template context provider validation failed: {exc}",
        )
    else:
        record_check(
            checks,
            errors,
            passed=True,
            description="template context providers validate",
        )

    try:
        template_sources = template_sources_from_modules(settings.modules)
    except CompositionError as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="configured template sources load",
            error=f"Configured template source validation failed: {exc}",
        )
        return ValidationResult(
            name="template",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    renderer = _template_renderer(settings, template_sources)

    for template_name in _template_names(settings, template_sources):
        template_resource = _template_resource(
            settings,
            template_sources,
            template_name,
        )
        if not record_check(
            checks,
            errors,
            passed=template_resource is not None,
            description=f"template exists: {template_name}",
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

    return ValidationResult(name="template", errors=tuple(errors), checks=tuple(checks))


def _contains_post_form(template_content: str) -> bool:
    parser = PostFormParser()
    parser.feed(template_content)
    parser.close()
    return parser.contains_post_form


def template_sources_for_validation(
    settings: TemplateValidationSettings,
) -> tuple[PackageResourceSource, ...]:
    return template_sources_from_modules(settings.modules)


def _template_renderer(
    settings: TemplateValidationSettings,
    template_sources: tuple[PackageResourceSource, ...],
) -> DefaultTemplateCapability:
    return DefaultTemplateCapability(
        template_root=(
            settings.template_root if _uses_filesystem_template_root(settings) else None
        ),
        template_sources=template_sources,
        auto_reload=settings.template_auto_reload,
        cache_size=settings.template_cache_size,
    )


def _template_names(
    settings: TemplateValidationSettings,
    template_sources: tuple[PackageResourceSource, ...],
) -> tuple[str, ...]:
    template_names: list[str] = []
    seen: set[str] = set()
    for template_name in _filesystem_template_names(settings):
        if template_name not in seen:
            seen.add(template_name)
            template_names.append(template_name)

    for source in template_sources:
        for template_name in _package_template_names(source):
            if template_name not in seen:
                seen.add(template_name)
                template_names.append(template_name)

    return tuple(template_names)


def _filesystem_template_names(settings: TemplateValidationSettings) -> tuple[str, ...]:
    if not _uses_filesystem_template_root(settings) or settings.template_root is None:
        return ()

    root = settings.template_root
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.html"))
        if path.is_file()
    )


def _package_template_names(source: PackageResourceSource) -> tuple[str, ...]:
    try:
        root = resources.files(source.package).joinpath(source.directory)
    except ModuleNotFoundError, TypeError:
        return ()
    if not root.is_dir():
        return ()

    return tuple(_traversable_template_names(root))


def _traversable_template_names(
    root: Traversable,
    prefix: str = "",
) -> tuple[str, ...]:
    template_names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        child_name = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            template_names.extend(_traversable_template_names(child, child_name))
        elif child.name.endswith(".html"):
            template_names.append(child_name)

    return tuple(template_names)


def _template_resource(
    settings: TemplateValidationSettings,
    template_sources: tuple[PackageResourceSource, ...],
    template_name: str,
) -> ResourceForValidation | None:
    if _uses_filesystem_template_root(settings) and settings.template_root is not None:
        template_path = settings.template_root / template_name
        if template_path.is_file():
            return template_path

    return first_existing_resource(template_sources, template_name)


def _read_template_content(
    template_resource: ResourceForValidation,
    checks: list[ValidationCheck],
    errors: list[str],
    *,
    description: str,
) -> str | None:
    try:
        return template_resource.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description=description,
            error=f"Unable to read {template_resource}: {exc}",
        )
        return None


def _template_location(settings: TemplateValidationSettings, template_name: str) -> str:
    if _uses_filesystem_template_root(settings) and settings.template_root is not None:
        return str(settings.template_root / template_name)

    return template_name


def _uses_filesystem_template_root(settings: TemplateValidationSettings) -> bool:
    return settings.uses_filesystem_template_root


validation_targets = {"template": validate_template}

__all__ = (
    "PostFormParser",
    "TemplateValidationSettings",
    "_contains_post_form",
    "template_sources_for_validation",
    "validate_template",
    "validation_targets",
)
