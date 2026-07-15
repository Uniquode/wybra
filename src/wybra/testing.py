"""Pytest-oriented helpers for testing Wybra applications and modules."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast

import httpx2
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient as FastAPITestClient
from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.auth.delivery import IdentityDelivery
from wybra.auth.models import User
from wybra.auth.persistence.contracts import LocalUserRecord
from wybra.config import MappingConfigSource
from wybra.db import DatabaseCapability
from wybra.db.capabilities import TortoiseDatabaseCapability
from wybra.db.migrate import apply_tortoise_migrations
from wybra.db.persistence import (
    Database,
    close_database,
    close_database_connections,
    create_database,
)
from wybra.db.settings import resolve_database_connection_from_config
from wybra.db.sql import ident, render_sql
from wybra.db.tortoise import build_tortoise_config
from wybra.db.urls import SQLITE_MEMORY_DATABASE_URL, is_memory_database_url
from wybra.messages import MessagesCapability
from wybra.messages.capabilities import RenderableAlerts
from wybra.messages.records import (
    ERROR_ALERT,
    SUCCESS_ALERT,
    WARNING_ALERT,
    AlertRecord,
)
from wybra.sessions.cleanup import SessionCleanupRegistry
from wybra.sessions.storage import MemorySessionStorage
from wybra.site import Site, SiteCapabilityError, get_site, start_site
from wybra.site_config import app_config_from_site

TEST_BASE_URL = "http://testserver"
MIGRATION_RECORDER_TABLE = "tortoise_migrations"


@dataclass(frozen=True, slots=True)
class MigratedTestDatabase:
    """An isolated SQLite database whose configured migrations are applied."""

    _connection: BaseDBAsyncClient = field(repr=False)
    _capability: DatabaseCapability = field(repr=False)
    database_url: str
    modules: tuple[str, ...]

    def connection(self) -> BaseDBAsyncClient:
        return self._connection

    def capability(self) -> DatabaseCapability:
        """Return the live database capability for direct module tests."""
        return self._capability

    async def clear(self) -> None:
        """Clear application tables while retaining the migrated schema."""
        connection = self.connection()
        _count, rows = await connection.execute_query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name <> ? "
            "ORDER BY name",
            [MIGRATION_RECORDER_TABLE],
        )
        table_names = tuple(
            name for row in rows if isinstance(name := row["name"], str)
        )
        if not table_names:
            return

        await connection.execute_script("PRAGMA foreign_keys = OFF;")
        try:
            for table_name in table_names:
                statement = render_sql(
                    t"DELETE FROM {ident(table_name)}",
                    dialect="sqlite",
                )
                await connection.execute_query(statement.statement)
        finally:
            await connection.execute_script("PRAGMA foreign_keys = ON;")


@asynccontextmanager
async def migrated_test_database(
    *,
    modules: Sequence[str],
    database_url: str | None = None,
) -> AsyncIterator[MigratedTestDatabase]:
    """Create an in-memory database and apply native Tortoise migrations.

    The context keeps its original Tortoise connection open for its full
    lifetime, allowing direct module tests to reuse the migrated schema.
    """
    module_names = tuple(modules)
    resolved_url = database_url or in_memory_database_url()
    database = await create_test_database(
        modules=module_names,
        database_url=resolved_url,
    )
    try:
        yield MigratedTestDatabase(
            _connection=database.connection(),
            _capability=TortoiseDatabaseCapability(
                database,
                {"default": "default", "reader": "default", "writer": "default"},
            ),
            database_url=resolved_url,
            modules=module_names,
        )
    finally:
        await close_database(database)


async def create_test_database(
    *,
    modules: Sequence[str],
    database_url: str | None = None,
) -> Database:
    """Create a test database and apply its configured native migrations.

    Use this when a test owns a non-default database lifecycle or needs an
    explicit SQLite file URL. Otherwise prefer :func:`migrated_test_database`.
    """
    module_names = tuple(modules)
    if not module_names:
        raise ValueError("Test database modules must not be empty.")
    database = await create_database(
        database_url or in_memory_database_url(),
        modules=module_names,
    )
    try:
        await migrate_test_database(database)
    except Exception:
        await close_database(database)
        raise
    return database


async def migrate_test_database(database: Database) -> None:
    """Apply native migrations to an existing test database."""
    apps = database.config.get("apps")
    if not isinstance(apps, dict):
        raise RuntimeError("Tortoise test database configuration has no apps.")
    await apply_tortoise_migrations(
        database.connection(),
        cast(dict[str, dict[str, object]], apps),
    )


def in_memory_database_url() -> str:
    """Return Tortoise's canonical in-memory SQLite URL for an isolated test."""
    return SQLITE_MEMORY_DATABASE_URL


def application_test_config(
    *,
    modules: Sequence[str],
    database_url: str | None = None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    """Build a minimal application configuration for end-to-end tests."""
    values: dict[str, dict[str, object]] = {
        "app": {
            "modules": tuple(modules),
            "database_url": database_url or in_memory_database_url(),
            "deployment_environment": "local",
        }
    }
    return configuration_with_overrides(values, overrides or {})


def create_test_application(
    config_values: Mapping[str, Mapping[str, object]],
    *,
    app: FastAPI | None = None,
) -> FastAPI:
    """Create a FastAPI application with Wybra lifecycle composition."""
    application = app or FastAPI()
    application.router.lifespan_context = start_site(
        config_source=MappingConfigSource(config_values)
    )
    return application


@dataclass(frozen=True, slots=True)
class MigratedTestApplication:
    """A composed application, its client, and its migrated database."""

    app: FastAPI
    client: httpx2.AsyncClient
    database: MigratedTestDatabase


class WybraTestClient(FastAPITestClient):
    """In-process client that manages a Wybra application's lifespan.

    Use it as a normal context manager for synchronous tests, or as an async
    context manager for tests marked with ``pytest.mark.anyio``.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        base_url: str = TEST_BASE_URL,
        raise_server_exceptions: bool = True,
        follow_redirects: bool = True,
    ) -> None:
        super().__init__(
            app,
            base_url=base_url,
            raise_server_exceptions=raise_server_exceptions,
            follow_redirects=follow_redirects,
        )
        self._app = app
        self._async_base_url = base_url
        self._async_raise_server_exceptions = raise_server_exceptions
        self._async_follow_redirects = follow_redirects
        self._async_lifespan: AbstractAsyncContextManager[None] | None = None
        self._async_client: httpx2.AsyncClient | None = None

    @contextmanager
    def _portal_factory(self):
        ad_hoc_request = self.portal is None
        with super()._portal_factory() as portal:
            if ad_hoc_request:
                portal.call(_prepare_ad_hoc_test_client_request, self.app)
            try:
                yield portal
            finally:
                if ad_hoc_request:
                    portal.call(_close_test_client_database_connections, self.app)

    async def __aenter__(self) -> httpx2.AsyncClient:
        lifespan = _lifespan_context(self._app)
        await lifespan.__aenter__()
        try:
            client = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(
                    app=self._app,
                    raise_app_exceptions=self._async_raise_server_exceptions,
                ),
                base_url=self._async_base_url,
                follow_redirects=self._async_follow_redirects,
            )
            await client.__aenter__()
        except BaseException as exc:
            await lifespan.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        self._async_lifespan = lifespan
        self._async_client = client
        return client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        client = self._async_client
        lifespan = self._async_lifespan
        self._async_client = None
        self._async_lifespan = None
        try:
            if client is not None:
                await client.__aexit__(exc_type, exc, traceback)
        finally:
            if lifespan is not None:
                await lifespan.__aexit__(exc_type, exc, traceback)


@asynccontextmanager
async def migrated_test_application(
    app: FastAPI,
    *,
    base_url: str = TEST_BASE_URL,
    raise_server_exceptions: bool = True,
    follow_redirects: bool = True,
) -> AsyncIterator[MigratedTestApplication]:
    """Compose an application and apply migrations to its live test database."""
    async with WybraTestClient(
        app,
        base_url=base_url,
        raise_server_exceptions=raise_server_exceptions,
        follow_redirects=follow_redirects,
    ) as client:
        database = await _migrate_application_database(app)
        yield MigratedTestApplication(app=app, client=client, database=database)


def configuration_with_overrides(
    values: Mapping[str, Mapping[str, object]],
    overrides: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Return a copied config mapping with per-section test overrides applied."""
    combined = {
        section: dict(section_values) for section, section_values in values.items()
    }
    for section, section_overrides in overrides.items():
        combined.setdefault(section, {}).update(section_overrides)
    return combined


def create_test_site(
    config_values: Mapping[str, Mapping[str, object]],
    *,
    app: FastAPI | None = None,
) -> Site:
    """Create an uncomposed site for direct module tests and test doubles."""
    from wybra.config import ConfigService

    application = app or FastAPI()
    site = Site(
        app=application,
        config=ConfigService(
            [MappingConfigSource(config_values)],
            discover_module_config=False,
        ),
    )
    application.state.site = site
    return site


def memory_session_storage(
    *,
    payload_max_bytes: int = 65_536,
    cleanup_registry: SessionCleanupRegistry | None = None,
) -> MemorySessionStorage:
    """Return an isolated in-memory session store for a direct module test."""
    return MemorySessionStorage(
        payload_max_bytes=payload_max_bytes,
        cleanup_registry=cleanup_registry,
    )


async def create_test_user(
    *,
    email: str | None = None,
    **values: object,
) -> User:
    """Create a verified, active local user in the current migrated database."""
    user_values: dict[str, object] = {
        "email": email or f"test-{uuid.uuid4().hex}@example.test",
        "is_active": True,
        "is_verified": True,
        "password_login_enabled": True,
    }
    user_values.update(values)
    return await User.create(**cast(dict[str, Any], user_values))


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    kind: str
    user: LocalUserRecord
    token: str
    request: Request | None


@dataclass(slots=True)
class RecordingIdentityDelivery(IdentityDelivery):
    """Capture identity delivery requests for assertion in tests."""

    deliveries: list[DeliveryRecord] = field(default_factory=list)

    async def send_reset_password_token(
        self,
        user: LocalUserRecord,
        token: str,
        request: Request | None = None,
    ) -> None:
        self.deliveries.append(DeliveryRecord("reset_password", user, token, request))

    async def send_verification_token(
        self,
        user: LocalUserRecord,
        token: str,
        request: Request | None = None,
    ) -> None:
        self.deliveries.append(DeliveryRecord("verification", user, token, request))


@dataclass(slots=True)
class RecordingMessages(MessagesCapability):
    """In-memory message capability for tests that inspect queued alerts."""

    alerts: list[AlertRecord] = field(default_factory=list)

    async def add_alert(
        self,
        request: Request,
        severity: str,
        message: object,
    ) -> None:
        del request
        self.alerts.append(
            AlertRecord.create(
                severity,
                message,
                max_message_length=10_000,
            )
        )

    async def success(self, request: Request, message: object) -> None:
        await self.add_alert(request, SUCCESS_ALERT, message)

    async def warning(self, request: Request, message: object) -> None:
        await self.add_alert(request, WARNING_ALERT, message)

    async def error(self, request: Request, message: object) -> None:
        await self.add_alert(request, ERROR_ALERT, message)

    async def peek_alerts(self, request: Request) -> tuple[AlertRecord, ...]:
        del request
        return tuple(self.alerts)

    async def acknowledge_alerts(self, request: Request) -> None:
        del request
        self.alerts.clear()

    async def renderable_alerts(self, request: Request) -> RenderableAlerts:
        return RenderableAlerts(request=request, alerts=tuple(self.alerts))

    async def consume_alerts(self, request: Request) -> tuple[AlertRecord, ...]:
        del request
        alerts = tuple(self.alerts)
        self.alerts.clear()
        return alerts

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        del session_data

    async def cleanup_expired(self, *, now: float) -> None:
        del now

    async def validate(self) -> None:
        return None


def _lifespan_context(app: FastAPI) -> AbstractAsyncContextManager[None]:
    lifespan_factory = getattr(app.router, "lifespan_context", None)
    if not callable(lifespan_factory):
        raise RuntimeError("Test application does not define an ASGI lifespan.")
    return cast(AbstractAsyncContextManager[None], lifespan_factory(app))


async def _close_test_client_database_connections(app: FastAPI) -> None:
    site = getattr(getattr(app, "state", None), "site", None)
    if site is None:
        return
    try:
        database = site.require_capability(DatabaseCapability)
    except SiteCapabilityError:
        return
    if not isinstance(database, TortoiseDatabaseCapability):
        return
    await close_database_connections(
        database._database,
        restore_create_connection=False,
    )


async def _prepare_ad_hoc_test_client_request(app: FastAPI) -> None:
    """Reset persistent connections before an ad-hoc sync portal request."""
    site = getattr(getattr(app, "state", None), "site", None)
    if site is None:
        return
    try:
        database = site.require_capability(DatabaseCapability)
    except SiteCapabilityError:
        return
    if not isinstance(database, TortoiseDatabaseCapability):
        return
    if _test_database_is_in_memory(database):
        raise RuntimeError(
            "Ad-hoc WybraTestClient requests cannot use an in-memory database. "
            "Use `async with WybraTestClient(app)` or the `wybra_test_client` "
            "fixture."
        )
    await close_database_connections(
        database._database,
        restore_create_connection=False,
    )


def _test_database_is_in_memory(database: TortoiseDatabaseCapability) -> bool:
    connections = database._database.config.get("connections")
    if not isinstance(connections, dict):
        return False
    default_connection = connections.get("default")
    return isinstance(default_connection, str) and is_memory_database_url(
        default_connection
    )


async def _migrate_application_database(app: FastAPI) -> MigratedTestDatabase:
    site = get_site(app)
    database = site.require_capability(DatabaseCapability)
    app_config = app_config_from_site(site)
    if app_config.database_url is None:
        raise RuntimeError("Test application must configure an in-memory database URL.")
    connection = resolve_database_connection_from_config(
        site.config,
        project_root=app_config.project_root,
        configured_database_url=app_config.database_url,
    )
    if connection is None:
        raise RuntimeError("Test application does not configure a database.")
    config = build_tortoise_config(
        database_connection=connection,
        modules=site.modules,
    )
    apps = config.get("apps")
    if not isinstance(apps, dict):
        raise RuntimeError("Tortoise test application configuration has no apps.")
    await apply_tortoise_migrations(
        database.connection(),
        cast(dict[str, dict[str, object]], apps),
    )
    return MigratedTestDatabase(
        _connection=database.connection(),
        _capability=database,
        database_url=app_config.database_url,
        modules=site.modules,
    )


__all__ = (
    "DeliveryRecord",
    "MIGRATION_RECORDER_TABLE",
    "MigratedTestApplication",
    "MigratedTestDatabase",
    "RecordingIdentityDelivery",
    "RecordingMessages",
    "TEST_BASE_URL",
    "configuration_with_overrides",
    "create_test_database",
    "create_test_user",
    "create_test_application",
    "create_test_site",
    "migrated_test_database",
    "migrated_test_application",
    "memory_session_storage",
    "migrate_test_database",
    "WybraTestClient",
    "in_memory_database_url",
    "application_test_config",
)
