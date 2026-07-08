"""Reusable Tortoise data infrastructure.

`wybra.db` may depend on Tortoise and shared composition contracts, but it must
not import host application settings, route modules, or startup code.
"""

from wybra.db.capabilities import (
    DatabaseCapability,
    DatabaseCapabilityError,
    TortoiseDatabaseCapability,
)
from wybra.db.config import module_config
from wybra.db.urls import (
    available_database_url_schemes,
    resolve_database_url,
    sqlite_database_path,
    sqlite_file_url,
    supported_database_url_schemes,
)
from wybra.db.validation import (
    PersistenceValidationSettings,
    validate_persistence,
)
from wybra.site import Site, SiteCapabilityError
from wybra.site_config import app_config_from_site


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    database_url = app_config.database_url
    if not isinstance(database_url, str) or not database_url.strip():
        raise SiteCapabilityError(
            "Database capability requires [app].database_url to be configured."
        )
    site.provide_capability(
        DatabaseCapability,
        await TortoiseDatabaseCapability.from_database_url(
            resolve_database_url(database_url, app_config.project_root),
            modules=site.modules,
        ),
    )


__all__ = (
    "DatabaseCapability",
    "DatabaseCapabilityError",
    "module_config",
    "PersistenceValidationSettings",
    "TortoiseDatabaseCapability",
    "available_database_url_schemes",
    "setup_site",
    "sqlite_database_path",
    "sqlite_file_url",
    "supported_database_url_schemes",
    "validate_persistence",
)
