"""Configured-module naming conventions shared by web, data, and tools.

This module is deliberately value-only: it must not import host application
packages, route modules, settings, databases, or runtime startup code.
"""

from typing import Final

CONTEXT_SURFACE_MODULE: Final = "context"
MODEL_METADATA_ATTRIBUTE: Final = "metadata"
MODEL_SURFACE_MODULE: Final = "models"
MIGRATION_RESOURCE_DIRECTORY: Final = "migrations"
ROUTE_EXPORT_ATTRIBUTE: Final = "module_routers"
ROUTE_MODULE: Final = "routes"
STATIC_RESOURCE_DIRECTORY: Final = "static"
TEMPLATE_RESOURCE_DIRECTORY: Final = "templates"
VALIDATION_SURFACE_MODULE: Final = "validation"
VALIDATION_TARGETS_ATTRIBUTE: Final = "validation_targets"


def module_surface_name(module_name: str, surface_module: str) -> str:
    return f"{module_name}.{surface_module}"


__all__ = (
    "CONTEXT_SURFACE_MODULE",
    "MIGRATION_RESOURCE_DIRECTORY",
    "MODEL_METADATA_ATTRIBUTE",
    "MODEL_SURFACE_MODULE",
    "ROUTE_EXPORT_ATTRIBUTE",
    "ROUTE_MODULE",
    "STATIC_RESOURCE_DIRECTORY",
    "TEMPLATE_RESOURCE_DIRECTORY",
    "VALIDATION_SURFACE_MODULE",
    "VALIDATION_TARGETS_ATTRIBUTE",
    "module_surface_name",
)
