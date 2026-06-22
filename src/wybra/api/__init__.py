"""Public API response behaviour module."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "API_CAPABILITY_MARKER": "wybra.api.capabilities",
    "ApiCapability": "wybra.api.capabilities",
    "ApiError": "wybra.api.capabilities",
    "ApiLinkMode": "wybra.api.config",
    "ApiMetadata": "wybra.api.capabilities",
    "ApiPageLink": "wybra.api.capabilities",
    "ApiPaging": "wybra.api.capabilities",
    "ApiSettings": "wybra.api.settings",
    "DefaultApiCapability": "wybra.api.capabilities",
    "api_provider_configured": "wybra.api.capabilities",
    "module_config": "wybra.api.config",
    "post_setup_site": "wybra.api.capabilities",
    "setup_site": "wybra.api.capabilities",
    "validate_api": "wybra.api.validation",
    "validation_targets": "wybra.api.validation",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.api' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "API_CAPABILITY_MARKER",
    "ApiCapability",
    "ApiError",
    "ApiLinkMode",
    "ApiMetadata",
    "ApiPageLink",
    "ApiPaging",
    "ApiSettings",
    "DefaultApiCapability",
    "api_provider_configured",
    "module_config",
    "post_setup_site",
    "setup_site",
    "validate_api",
    "validation_targets",
]
