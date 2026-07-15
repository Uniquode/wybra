from contextlib import AbstractAsyncContextManager
from pathlib import Path

from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_transaction
from wybra.db.urls import sqlite_file_url

__all__ = ("database_write_transaction", "sqlite_file_url", "sync_sqlite_file_url")


def database_write_transaction(
    database: DatabaseCapability,
) -> AbstractAsyncContextManager[BaseDBAsyncClient]:
    """Return a test transaction bound to the default writer route."""
    return tortoise_transaction(database, database.database().for_write())


def sync_sqlite_file_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"
