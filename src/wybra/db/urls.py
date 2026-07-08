import importlib.util
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

SQLITE_DATABASE_URL_PREFIX = "sqlite:///"
SQLITE_MEMORY_DATABASE_URL = "sqlite://:memory:"
POSTGRESQL_TORTOISE_DATABASE_URL_SCHEME = "asyncpg"
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


@dataclass(frozen=True, slots=True)
class DatabaseBackend:
    scheme: str
    tortoise_scheme: str
    required_module_groups: tuple[tuple[str, ...], ...]


DATABASE_BACKENDS = (
    DatabaseBackend(
        scheme="sqlite",
        tortoise_scheme="sqlite",
        required_module_groups=(("aiosqlite",),),
    ),
    DatabaseBackend(
        scheme="postgresql",
        tortoise_scheme=POSTGRESQL_TORTOISE_DATABASE_URL_SCHEME,
        required_module_groups=(("asyncpg",),),
    ),
    DatabaseBackend(
        scheme="postgres",
        tortoise_scheme=POSTGRESQL_TORTOISE_DATABASE_URL_SCHEME,
        required_module_groups=(("asyncpg",),),
    ),
    DatabaseBackend(
        scheme="asyncpg",
        tortoise_scheme=POSTGRESQL_TORTOISE_DATABASE_URL_SCHEME,
        required_module_groups=(("asyncpg",),),
    ),
    DatabaseBackend(
        scheme="psycopg",
        tortoise_scheme="psycopg",
        required_module_groups=(("psycopg", "psycopg_pool"),),
    ),
    DatabaseBackend(
        scheme="mysql",
        tortoise_scheme="mysql",
        required_module_groups=(("asyncmy",), ("aiomysql",)),
    ),
    DatabaseBackend(
        scheme="mssql",
        tortoise_scheme="mssql",
        required_module_groups=(("asyncodbc", "pyodbc"),),
    ),
    DatabaseBackend(
        scheme="oracle",
        tortoise_scheme="oracle",
        required_module_groups=(("asyncodbc", "pyodbc"),),
    ),
)
SUPPORTED_DATABASE_URL_SCHEMES = tuple(backend.scheme for backend in DATABASE_BACKENDS)
DATABASE_URL_TEXT_PATTERN = re.compile(
    rf"(?:{'|'.join(re.escape(scheme) for scheme in SUPPORTED_DATABASE_URL_SCHEMES)})"
    r"://[^\s]+"
)


@dataclass(frozen=True, slots=True)
class SqliteDatabaseUrl:
    path: Path
    is_absolute: bool = False
    query: str = ""
    fragment: str = ""

    @property
    def suffix(self) -> str:
        value = f"?{self.query}" if self.query else ""
        if self.fragment:
            value = f"{value}#{self.fragment}"

        return value


def is_supported_database_url(database_url: str) -> bool:
    backend = database_backend_for_url(database_url)
    return backend is not None and is_database_backend_available(backend)


def database_url_support_error() -> str:
    return (
        "Database URL must use an available Tortoise database scheme: "
        f"{_database_url_scheme_list()}."
    )


def supported_database_url_schemes() -> tuple[str, ...]:
    return SUPPORTED_DATABASE_URL_SCHEMES


def available_database_url_schemes() -> tuple[str, ...]:
    return tuple(
        backend.scheme
        for backend in DATABASE_BACKENDS
        if is_database_backend_available(backend)
    )


def database_backend_for_url(database_url: str) -> DatabaseBackend | None:
    try:
        scheme = urlsplit(database_url).scheme
    except ValueError:
        return None
    return database_backend_for_scheme(scheme)


def database_backend_for_scheme(scheme: str) -> DatabaseBackend | None:
    for backend in DATABASE_BACKENDS:
        if backend.scheme == scheme:
            return backend
    return None


def is_database_backend_available(backend: DatabaseBackend) -> bool:
    return any(
        all(importlib.util.find_spec(module_name) is not None for module_name in group)
        for group in backend.required_module_groups
    )


def tortoise_database_url(database_url: str) -> str:
    backend = database_backend_for_url(database_url)
    if backend is None:
        return database_url

    parsed = urlsplit(database_url)
    if parsed.scheme == backend.tortoise_scheme:
        return database_url

    return urlunsplit(
        SplitResult(
            scheme=backend.tortoise_scheme,
            netloc=parsed.netloc,
            path=parsed.path,
            query=parsed.query,
            fragment=parsed.fragment,
        )
    )


def _database_url_scheme_list() -> str:
    return ", ".join(f"{scheme}://" for scheme in available_database_url_schemes())


def is_memory_database_url(database_url: str) -> bool:
    return database_url == SQLITE_MEMORY_DATABASE_URL


def parse_sqlite_database_url(database_url: str) -> SqliteDatabaseUrl | None:
    """Parse supported sqlite URLs without authority components.

    URL text determines path absoluteness so the same configuration resolves
    consistently on POSIX and Windows hosts.
    """

    if is_memory_database_url(database_url):
        return None

    if not database_url.startswith(SQLITE_DATABASE_URL_PREFIX):
        return None

    parsed = urlsplit(database_url)
    if parsed.scheme != "sqlite" or parsed.netloc or not parsed.path:
        return None

    raw_path = parsed.path
    if not raw_path.startswith("/"):
        return None

    leading_slashes = len(raw_path) - len(raw_path.lstrip("/"))
    if leading_slashes == 1:
        path = raw_path.removeprefix("/")
        is_absolute = _is_windows_absolute_path_text(path)
    else:
        path = f"/{raw_path.lstrip('/')}"
        is_absolute = True

    return SqliteDatabaseUrl(
        path=Path(unquote(path)),
        is_absolute=is_absolute,
        query=parsed.query,
        fragment=parsed.fragment,
    )


def resolve_database_url(database_url: str, project_root: Path) -> str:
    sqlite_url = parse_sqlite_database_url(database_url)
    if sqlite_url is None:
        return database_url

    database_path = sqlite_url.path
    if sqlite_url.is_absolute:
        return database_url
    database_path = project_root / database_path

    return f"{sqlite_file_url(database_path)}{sqlite_url.suffix}"


def sqlite_file_url(path: Path) -> str:
    return f"{SQLITE_DATABASE_URL_PREFIX}{path.resolve().as_posix()}"


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


def safe_database_error_message(exc: BaseException) -> str:
    return redact_database_urls(str(exc))


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


def _is_windows_absolute_path_text(path: str) -> bool:
    return re.match(r"^[A-Za-z]:(?:/|\\)", path) is not None
