"""Reusable SQLAlchemy and Alembic data infrastructure.

`wevra.db` may depend on SQLAlchemy, Alembic, and shared composition contracts,
but it must not import host application settings, route modules, or startup code.
"""

from wevra.db.capabilities import (
    DatabaseCapability,
    DatabaseCapabilityError,
    SqlAlchemyDatabaseCapability,
)
from wevra.db.validation import (
    PersistenceValidationSettings,
    validate_persistence,
)
from wevra.site import Site, SiteCapabilityError


def setup_site(site: Site) -> None:
    app_config = site.config.get_config("app") or {}
    database_url = app_config.get("database_url")
    if not isinstance(database_url, str) or not database_url.strip():
        raise SiteCapabilityError(
            "Database capability requires [app].database_url to be configured."
        )
    site.provide_capability(
        DatabaseCapability,
        SqlAlchemyDatabaseCapability.from_database_url(database_url),
    )


__all__ = (
    "DatabaseCapability",
    "DatabaseCapabilityError",
    "PersistenceValidationSettings",
    "SqlAlchemyDatabaseCapability",
    "setup_site",
    "validate_persistence",
)
