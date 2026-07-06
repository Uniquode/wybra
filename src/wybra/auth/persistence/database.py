from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wybra.core.exceptions import ConfigurationError
from wybra.db.urls import (
    SQLITE_ASYNC_DATABASE_URL_PREFIX,
    SQLITE_MEMORY_DATABASE_URL,
    SUPPORTED_DATABASE_URL_PREFIXES,
    SqliteDatabaseUrl,
    is_memory_database_url,
    is_supported_database_url,
    parse_sqlite_database_url,
)
from wybra.db.urls import (
    resolve_database_url as resolve_config_database_url,
)
from wybra.diagnostics.sqlalchemy import instrument_sqlalchemy_engine

__all__ = (
    "Database",
    "SQLITE_ASYNC_DATABASE_URL_PREFIX",
    "SQLITE_MEMORY_DATABASE_URL",
    "SUPPORTED_DATABASE_URL_PREFIXES",
    "SqliteDatabaseUrl",
    "close_database",
    "create_database",
    "create_database_engine",
    "create_session_factory",
    "is_memory_database_url",
    "is_supported_database_url",
    "parse_sqlite_database_url",
    "resolve_database_url",
    "session_scope",
)


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def resolve_database_url(database_url: str, base_path: Path) -> str:
    if not database_url.strip():
        raise ConfigurationError("Auth database URL must not be blank.")

    if not is_supported_database_url(database_url):
        raise ConfigurationError(
            "Auth database URL uses an unsupported scheme; "
            "must use sqlite+aiosqlite or postgresql+asyncpg."
        )

    if database_url.startswith("sqlite+aiosqlite:"):
        sqlite_url = parse_sqlite_database_url(database_url)
        if sqlite_url is None and not is_memory_database_url(database_url):
            raise ConfigurationError(
                "Auth sqlite database URL must use "
                "sqlite+aiosqlite:///relative.db or "
                "sqlite+aiosqlite:////absolute/path.db; authority forms such as "
                "sqlite+aiosqlite://host/path are not supported."
            )

    return resolve_config_database_url(database_url, base_path)


def create_database_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    instrument_sqlalchemy_engine(engine)
    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def create_database(database_url: str) -> Database:
    engine = create_database_engine(database_url)
    return Database(engine=engine, session_factory=create_session_factory(engine))


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def close_database(database: Database | AsyncEngine) -> None:
    engine = database.engine if isinstance(database, Database) else database
    await engine.dispose()
