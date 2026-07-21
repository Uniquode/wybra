"""Reusable Tortoise data infrastructure.

`wybra.db` may depend on Tortoise and shared composition contracts, but it must
not import host application settings, route modules, or startup code.
"""

from wybra.db.capabilities import (
    DatabaseCapability,
    DatabaseCapabilityError,
)
from wybra.db.capabilities import (
    WybraDatabaseCapability as _WybraDatabaseCapability,
)
from wybra.db.config import module_config
from wybra.db.routing import DbConnection, DbRoute
from wybra.db.settings import (
    EffectiveDatabaseConfig,
    ResolvedDatabaseConnection,
    ResolvedDatabaseRouting,
    StructuredDatabaseConfig,
    resolve_database_connection_from_config,
    resolve_database_provisioning_connection_from_config,
    resolve_database_routing_from_config,
)
from wybra.db.urls import (
    available_database_url_schemes,
    sqlite_database_path,
    sqlite_file_url,
    supported_database_url_schemes,
)
from wybra.db.validation import (
    PersistenceValidationSettings,
    validate_persistence,
)
from wybra.db.versioning import (
    PositiveBigIntField,
    PositiveIntField,
    VersionField,
    VersionFieldError,
)
from wybra.site import Site, SiteCapabilityError
from wybra.site_config import app_config_from_site


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    database_routing = resolve_database_routing_from_config(
        site.config,
        project_root=app_config.project_root,
        configured_database_url=app_config.database_url,
    )
    if database_routing is None:
        raise SiteCapabilityError(
            "Database capability requires [app.database] or [app].database_url "
            "to be configured."
        )
    site.provide_capability(
        DatabaseCapability,
        await _WybraDatabaseCapability.from_database_routing(
            database_routing,
            modules=site.modules,
        ),
    )


__all__ = (
    "DatabaseCapability",
    "DatabaseCapabilityError",
    "DbConnection",
    "DbRoute",
    "EffectiveDatabaseConfig",
    "module_config",
    "PersistenceValidationSettings",
    "PositiveIntField",
    "PositiveBigIntField",
    "ResolvedDatabaseConnection",
    "ResolvedDatabaseRouting",
    "StructuredDatabaseConfig",
    "available_database_url_schemes",
    "resolve_database_connection_from_config",
    "resolve_database_routing_from_config",
    "resolve_database_provisioning_connection_from_config",
    "setup_site",
    "sqlite_database_path",
    "sqlite_file_url",
    "supported_database_url_schemes",
    "validate_persistence",
    "VersionField",
    "VersionFieldError",
)
