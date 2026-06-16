"""Reusable SQLAlchemy and Alembic data infrastructure.

`wybra.db` may depend on SQLAlchemy, Alembic, and shared composition contracts,
but it must not import host application settings, route modules, or startup code.
"""

from wybra.db.capabilities import (
    DatabaseCapability,
    DatabaseCapabilityError,
    SqlAlchemyDatabaseCapability,
)
from wybra.db.config import module_config
from wybra.db.urls import resolve_database_url
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
        SqlAlchemyDatabaseCapability.from_database_url(
            resolve_database_url(database_url, app_config.project_root)
        ),
    )


__all__ = (
    "DatabaseCapability",
    "DatabaseCapabilityError",
    "module_config",
    "PersistenceValidationSettings",
    "SqlAlchemyDatabaseCapability",
    "setup_site",
    "validate_persistence",
)
