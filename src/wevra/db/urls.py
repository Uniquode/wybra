import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import (
    SplitResult,
    parse_qsl,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

SQLITE_ASYNC_DATABASE_URL_PREFIX = "sqlite+aiosqlite:///"
SQLITE_MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
SUPPORTED_DATABASE_URL_PREFIXES = (
    "sqlite+aiosqlite://",
    "postgresql+asyncpg://",
)
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "access_token",
        "apikey",
        "api_key",
        "key",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    }
)
DATABASE_URL_TEXT_PATTERN = re.compile(
    r"(?:sqlite\+aiosqlite|postgresql(?:\+[A-Za-z0-9_]+)?)://[^\s]+"
)


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
    if is_memory_database_url(database_url):
        return None

    if not database_url.startswith(SQLITE_ASYNC_DATABASE_URL_PREFIX):
        return None

    parsed = urlsplit(database_url)
    if parsed.scheme != "sqlite+aiosqlite" or parsed.netloc or not parsed.path:
        return None

    path = parsed.path
    if path.startswith("//"):
        path = path[1:]
    else:
        path = path.removeprefix("/")

    return SqliteDatabaseUrl(
        path=Path(unquote(path)),
        query=parsed.query,
        fragment=parsed.fragment,
    )


def resolve_database_url(database_url: str, project_root: Path) -> str:
    sqlite_url = parse_sqlite_database_url(database_url)
    if sqlite_url is None:
        return database_url

    database_path = sqlite_url.path
    if not database_path.is_absolute():
        database_path = project_root / database_path

    return (
        f"{SQLITE_ASYNC_DATABASE_URL_PREFIX}"
        f"{database_path.resolve().as_posix()}{sqlite_url.suffix}"
    )


def sqlite_database_path(database_url: str) -> Path | None:
    sqlite_url = parse_sqlite_database_url(database_url)
    return sqlite_url.path if sqlite_url is not None else None


def redact_database_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value

    query = _redact_query(parsed.query)
    if not parsed.scheme or (parsed.username is None and parsed.password is None):
        return urlunsplit(
            SplitResult(
                scheme=parsed.scheme,
                netloc=parsed.netloc,
                path=parsed.path,
                query=query,
                fragment=parsed.fragment,
            )
        )

    credentials = "***:***" if parsed.password is not None else "***"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"

    netloc = f"{credentials}@{host}"
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=netloc,
            path=parsed.path,
            query=query,
            fragment=parsed.fragment,
        )
    )


def redact_database_urls(value: str) -> str:
    return DATABASE_URL_TEXT_PATTERN.sub(
        lambda match: redact_database_url(match.group(0)),
        value,
    )


def _redact_query(query: str) -> str:
    if not query:
        return query

    query_items = parse_qsl(query, keep_blank_values=True)
    if not any(name.lower() in SENSITIVE_QUERY_PARAMS for name, _value in query_items):
        return query

    redacted_items = [
        (name, "***") if name.lower() in SENSITIVE_QUERY_PARAMS else (name, value)
        for name, value in query_items
    ]
    return urlencode(redacted_items)
