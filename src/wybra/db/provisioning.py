from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from wybra.db.urls import safe_database_error_message

ADMIN_DATABASE_URL_ENV = "SA_DATABASE_URL"
POSTGRESQL_ASYNC_SCHEME = "postgresql+asyncpg"
POSTGRESQL_SYNC_SCHEME = "postgresql"
POSTGRESQL_SCHEMES = frozenset({POSTGRESQL_ASYNC_SCHEME, POSTGRESQL_SYNC_SCHEME})


class DatabaseProvisioningError(RuntimeError):
    """Raised when database infrastructure provisioning fails."""


def is_postgresql_database_url(database_url: str) -> bool:
    return urlsplit(database_url).scheme in POSTGRESQL_SCHEMES


def provision_postgresql_database(
    database_url: str,
    admin_database_url: str | None = None,
) -> None:
    if admin_database_url is not None and not admin_database_url.strip():
        raise DatabaseProvisioningError("--admin-database-url must not be blank.")

    admin_url = admin_database_url or os.environ.get(ADMIN_DATABASE_URL_ENV)
    if admin_url is not None and not admin_url.strip():
        raise DatabaseProvisioningError(f"{ADMIN_DATABASE_URL_ENV} must not be blank.")

    if not admin_url:
        raise DatabaseProvisioningError(
            "PostgreSQL init requires --admin-database-url or SA_DATABASE_URL "
            "for database, user, role, and privilege provisioning."
        )

    try:
        dbscripts = _dbscripts_dblib()
        db = dbscripts.pg_db_info(url=_postgresql_sync_url(database_url))
        with _temporary_env(ADMIN_DATABASE_URL_ENV, _postgresql_sync_url(admin_url)):
            dbscripts.pg_setup(db)
    except DatabaseProvisioningError:
        raise
    except Exception as exc:
        raise DatabaseProvisioningError(safe_database_error_message(exc)) from exc


def _postgresql_sync_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if parsed.scheme not in POSTGRESQL_SCHEMES:
        raise DatabaseProvisioningError(
            "PostgreSQL provisioning requires a postgresql database URL."
        )
    if parsed.scheme == POSTGRESQL_SYNC_SCHEME:
        return database_url
    return urlunsplit(
        SplitResult(
            scheme=POSTGRESQL_SYNC_SCHEME,
            netloc=parsed.netloc,
            path=parsed.path,
            query=parsed.query,
            fragment=parsed.fragment,
        )
    )


def _dbscripts_dblib() -> Any:
    return import_module("dbscripts.dblib")


@contextmanager
def _temporary_env(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
