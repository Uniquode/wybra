from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from auth_ext.configuration import ConfigurationError

SQLITE_ASYNC_DATABASE_URL_PREFIX = "sqlite+aiosqlite:///"
SQLITE_MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
SUPPORTED_DATABASE_URL_PREFIXES = (
    "sqlite+aiosqlite://",
    "postgresql+asyncpg://",
)


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class SqliteDatabaseUrl:
    path: Path
    query: str = ""
    fragment: str = ""

    @property
    def suffix(self) -> str:
        value = f"?{self.query}" if self.query else ""
        if self.fragment:
            value = f"{value}#{self.fragment}"

        return value


def is_supported_database_url(database_url: str) -> bool:
    return database_url.startswith(SUPPORTED_DATABASE_URL_PREFIXES)


def is_memory_database_url(database_url: str) -> bool:
    return database_url == SQLITE_MEMORY_DATABASE_URL


def parse_sqlite_database_url(database_url: str) -> SqliteDatabaseUrl | None:
    """Parse supported sqlite+aiosqlite URLs without authority components.

    Supported path forms are explicit:

    - ``sqlite+aiosqlite:///relative.db`` maps to ``relative.db``.
    - ``sqlite+aiosqlite:////tmp/db.sqlite`` maps to ``/tmp/db.sqlite``.

    Authority/netloc forms such as ``sqlite+aiosqlite://host/path`` are not
    accepted, avoiding UNC-like ambiguity in package-owned configuration.
    """

    if is_memory_database_url(database_url):
        return None

    if not database_url.startswith(SQLITE_ASYNC_DATABASE_URL_PREFIX):
        return None

    parsed = urlsplit(database_url)
    if parsed.scheme != "sqlite+aiosqlite" or parsed.netloc or not parsed.path:
        return None

    raw_path = parsed.path
    if not raw_path.startswith("/"):
        return None

    leading_slashes = len(raw_path) - len(raw_path.lstrip("/"))
    if leading_slashes == 1:
        path = raw_path.removeprefix("/")
    else:
        path = f"/{raw_path.lstrip('/')}"

    return SqliteDatabaseUrl(
        path=Path(unquote(path)),
        query=parsed.query,
        fragment=parsed.fragment,
    )


def resolve_database_url(database_url: str, base_path: Path) -> str:
    if not database_url.strip():
        raise ConfigurationError("Auth database URL must not be blank.")

    if not is_supported_database_url(database_url):
        raise ConfigurationError(
            "Auth database URL must use sqlite+aiosqlite or postgresql+asyncpg."
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
    else:
        sqlite_url = None

    if sqlite_url is None:
        return database_url

    database_path = sqlite_url.path
    if not database_path.is_absolute():
        database_path = base_path / database_path

    return (
        f"{SQLITE_ASYNC_DATABASE_URL_PREFIX}"
        f"{database_path.resolve().as_posix()}{sqlite_url.suffix}"
    )


def create_database_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


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
