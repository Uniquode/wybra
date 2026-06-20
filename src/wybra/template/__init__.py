"""Public template rendering API."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "DefaultTemplateCapability": "wybra.template.capabilities",
    "TemplateCapability": "wybra.template.capabilities",
    "TemplateSettings": "wybra.template.settings",
    "build_template_loader": "wybra.template.templating",
    "discover_template_sources": "wybra.template.discovery",
    "module_config": "wybra.template.config",
    "render_page": "wybra.template.rendering",
    "render_partial": "wybra.template.rendering",
    "route_template": "wybra.template.metadata",
    "setup_site": "wybra.template.setup",
    "template_capability_from": "wybra.template.rendering",
    "template_sources_from_modules": "wybra.template.discovery",
    "validate_template": "wybra.template.validation",
    "validation_targets": "wybra.template.validation",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.template' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "DefaultTemplateCapability",
    "TemplateCapability",
    "TemplateSettings",
    "build_template_loader",
    "discover_template_sources",
    "module_config",
    "render_page",
    "render_partial",
    "route_template",
    "setup_site",
    "template_capability_from",
    "template_sources_from_modules",
    "validate_template",
    "validation_targets",
]
