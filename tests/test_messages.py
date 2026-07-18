from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.resources import PackageResourceSource
from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_transaction
from wybra.db.surfaces import (
    discover_migration_version_locations,
    discover_model_package,
)
from wybra.messages import (
    ERROR_ALERT,
    SUCCESS_ALERT,
    WARNING_ALERT,
    DefaultMessagesCapability,
    InvalidAlertError,
    MessageQueueUnavailableError,
    MessagesCapability,
    MessagesSettings,
    MessageStorageBackend,
    MessageStorageError,
)
from wybra.messages.config import module_config
from wybra.messages.context import messages_context
from wybra.messages.models import MessageAlert
from wybra.messages.records import AlertRecord
from wybra.messages.storage import (
    REQUEST_ALERTS_RENDERED_ATTRIBUTE,
    SESSION_ALERTS_KEY,
    SESSION_QUEUE_ID_KEY,
    DatabaseMessagesStorage,
    RedisCacheQueueBackend,
    SessionMessagesStorage,
    storage_from_settings,
)
from wybra.messages.validation import validate_alerts
from wybra.sessions import (
    DatabaseSessionStorage as DatabaseRequestSessionStorage,
)
from wybra.sessions import (
    SessionCleanupRegistry,
    SessionRecord,
    create_session_id,
)
from wybra.site import Site, start
from wybra.template.capabilities import DefaultTemplateCapability
from wybra.template.context import TemplateContext
from wybra.testing import create_test_site, migrated_test_database
from wybra.tools.validation.registry import discover_validation_targets


def _settings(values: dict[str, object] | None = None) -> MessagesSettings:
    return MessagesSettings.load_settings(
        {
            "wybra.messages": {} if values is None else values,
        }
    )


def _request(session: dict[str, object] | None = None) -> Request:
    app = FastAPI()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/target",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": app,
    }
    if session is not None:
        scope["session"] = session
    return Request(scope)


def _site(settings: MessagesSettings, capability: DefaultMessagesCapability) -> Site:
    app = FastAPI()
    site = Site(
        app=app,
        config=ConfigService(
            [MappingConfigSource({"wybra.messages": {}})],
            config_defs=(module_config,),
            discover_module_config=False,
        ),
    )
    app.state.site = site
    app.state.messages_settings = settings
    site.provide_capability(MessagesCapability, capability)
    return site


@asynccontextmanager
async def _database_site(
    *,
    modules: tuple[str, ...] = ("wybra.messages",),
) -> AsyncIterator[tuple[Site, DatabaseCapability]]:
    async with migrated_test_database(modules=modules) as database:
        site = create_test_site({"app": {"modules": modules}})
        capability = database.capability()
        site.provide_capability(DatabaseCapability, capability)
        yield site, capability


@pytest.mark.anyio
async def test_messages_module_registers_capability_on_startup() -> None:
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {"modules": ("wybra.messages",)},
                "wybra.messages": {},
            }
        ),
    )

    assert site.has_capability(MessagesCapability) is True
    assert isinstance(site.require_capability(MessagesCapability), MessagesCapability)


def test_messages_settings_defaults_to_session_storage() -> None:
    settings = _settings()

    assert settings.storage_backend is MessageStorageBackend.SESSION
    assert settings.queue_depth == 20


def test_alert_record_validates_severity_and_message() -> None:
    alert = AlertRecord.create(
        SUCCESS_ALERT,
        "Saved",
        max_message_length=20,
        created_at=1.0,
    )

    assert alert.severity == SUCCESS_ALERT
    assert alert.message == "Saved"
    assert alert.created_at == 1.0

    with pytest.raises(InvalidAlertError, match="severity"):
        AlertRecord.create("notice", "Saved", max_message_length=20)

    with pytest.raises(InvalidAlertError, match="blank"):
        AlertRecord.create(SUCCESS_ALERT, "   ", max_message_length=20)

    with pytest.raises(InvalidAlertError, match="maximum length"):
        AlertRecord.create(SUCCESS_ALERT, "x" * 21, max_message_length=20)


@pytest.mark.anyio
async def test_session_storage_queues_and_pops_alerts_once() -> None:
    settings = _settings()
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))
    session: dict[str, object] = {}
    request = _request(session)

    await capability.success(request, "Saved")
    await capability.warning(request, "Check this")
    alerts = await capability.consume_alerts(request)
    repeated_alerts = await capability.consume_alerts(request)

    assert [alert.severity for alert in alerts] == [SUCCESS_ALERT, WARNING_ALERT]
    assert [alert.message for alert in alerts] == ["Saved", "Check this"]
    assert repeated_alerts == alerts
    assert "_wybra_messages_alerts" not in session


@pytest.mark.anyio
async def test_session_storage_consume_after_peek_acknowledges_alerts() -> None:
    settings = _settings()
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))
    session: dict[str, object] = {}
    request = _request(session)

    await capability.success(request, "Saved")
    peeked_alerts = await capability.peek_alerts(request)

    assert [alert.message for alert in peeked_alerts] == ["Saved"]
    assert SESSION_ALERTS_KEY in session

    consumed_alerts = await capability.consume_alerts(request)
    repeated_alerts = await capability.consume_alerts(request)

    assert consumed_alerts == peeked_alerts
    assert repeated_alerts == consumed_alerts
    assert SESSION_ALERTS_KEY not in session


@pytest.mark.anyio
async def test_message_added_after_rendered_alerts_is_not_acknowledged() -> None:
    settings = _settings()
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))
    session: dict[str, object] = {}
    request = _request(session)

    await capability.success(request, "Rendered")
    alerts = await capability.renderable_alerts(request)

    assert [alert.message for alert in alerts] == ["Rendered"]
    assert getattr(request.state, REQUEST_ALERTS_RENDERED_ATTRIBUTE) is True

    await capability.success(request, "Queued later")

    repeated_alerts = await capability.peek_alerts(request)
    assert [alert.message for alert in repeated_alerts] == ["Queued later"]
    assert not hasattr(request.state, REQUEST_ALERTS_RENDERED_ATTRIBUTE)


@pytest.mark.anyio
async def test_session_storage_requires_request_session_mapping() -> None:
    settings = _settings()
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))

    with pytest.raises(MessageQueueUnavailableError, match="Wybra sessions"):
        await capability.error(_request(), "Cannot store")


@pytest.mark.anyio
async def test_queue_depth_discards_oldest_session_alerts() -> None:
    settings = _settings({"queue_depth": 2})
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))
    request = _request({})

    await capability.success(request, "One")
    await capability.warning(request, "Two")
    await capability.error(request, "Three")
    alerts = await capability.consume_alerts(request)

    assert [alert.message for alert in alerts] == ["Two", "Three"]


@pytest.mark.anyio
async def test_memory_cache_storage_persists_and_pops_alerts() -> None:
    settings = _settings({"storage_backend": "cache", "cache_url": "memory://alerts"})
    storage = storage_from_settings(
        Site(FastAPI(), ConfigService([], discover_module_config=False)),
        settings,
    )
    capability = DefaultMessagesCapability(settings, storage)
    session: dict[str, object] = {}
    first_request = _request(session)
    second_request = _request(session)

    await capability.success(first_request, "Cached")

    alerts = await capability.consume_alerts(second_request)
    empty_alerts = await capability.consume_alerts(_request(session))

    assert [alert.message for alert in alerts] == ["Cached"]
    assert empty_alerts == ()


@pytest.mark.anyio
async def test_cache_storage_reports_unavailable_backend() -> None:
    class BrokenRedisClient:
        async def ping(self) -> None:
            raise OSError("cache unavailable")

    backend = RedisCacheQueueBackend("redis://cache.example/0")
    backend._client = BrokenRedisClient()

    with pytest.raises(MessageStorageError, match="unavailable"):
        await backend.validate()


@pytest.mark.anyio
async def test_redis_cache_storage_clamps_subsecond_ttl() -> None:
    class FakeRedisClient:
        def __init__(self) -> None:
            self.expires: list[int] = []

        async def eval(
            self,
            script: str,
            key_count: int,
            queue_key: str,
            payload: str,
            queue_depth: str,
            ttl_seconds: str,
        ) -> int:
            self.expires.append(int(ttl_seconds))
            return 1

    client = FakeRedisClient()
    backend = RedisCacheQueueBackend("redis://cache.example/0")
    backend._client = client

    await backend.append(
        "queue",
        {"severity": SUCCESS_ALERT, "message": "Saved", "created_at": 1.0},
        queue_depth=1,
        ttl_seconds=0.5,
    )

    assert client.expires == [1]


@pytest.mark.anyio
async def test_redis_cache_storage_pops_queue_with_atomic_script() -> None:
    class FakeRedisClient:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def eval(self, script: str, key_count: int, queue_key: str) -> str:
            self.calls.append((key_count, queue_key))
            return json.dumps(
                [{"severity": SUCCESS_ALERT, "message": "Saved", "created_at": 1.0}]
            )

    client = FakeRedisClient()
    backend = RedisCacheQueueBackend("redis://cache.example/0")
    backend._client = client

    alerts = await backend.pop("queue")

    assert client.calls == [(1, "queue")]
    assert alerts == (
        {"severity": SUCCESS_ALERT, "message": "Saved", "created_at": 1.0},
    )


@pytest.mark.anyio
async def test_database_storage_persists_and_pops_alerts() -> None:
    settings = _settings({"storage_backend": "database"})
    async with _database_site() as (site, _db_capability):
        storage = DatabaseMessagesStorage(
            settings,
            site.capability_proxy(DatabaseCapability),
        )
        capability = DefaultMessagesCapability(settings, storage)
        session: dict[str, object] = {}
        await capability.error(_request(session), "Stored")
        alerts = await capability.consume_alerts(_request(session))
        empty_alerts = await capability.consume_alerts(_request(session))

    assert [alert.severity for alert in alerts] == [ERROR_ALERT]
    assert [alert.message for alert in alerts] == ["Stored"]
    assert empty_alerts == ()


@pytest.mark.anyio
async def test_database_storage_queue_depth_keeps_newest_alerts() -> None:
    settings = _settings({"storage_backend": "database", "queue_depth": 2})
    async with _database_site() as (site, _db_capability):
        capability = DefaultMessagesCapability(
            settings,
            DatabaseMessagesStorage(
                settings,
                site.capability_proxy(DatabaseCapability),
            ),
        )
        session: dict[str, object] = {}
        await capability.success(_request(session), "One")
        await capability.warning(_request(session), "Two")
        await capability.error(_request(session), "Three")
        alerts = await capability.consume_alerts(_request(session))

    assert [alert.message for alert in alerts] == ["Two", "Three"]


@pytest.mark.anyio
async def test_database_storage_removes_alert_queue_when_session_is_deleted() -> None:
    settings = _settings({"storage_backend": "database"})
    async with _database_site(
        modules=("wybra.messages", "wybra.sessions"),
    ) as (site, db_capability):
        messages = DefaultMessagesCapability(
            settings,
            DatabaseMessagesStorage(
                settings,
                site.capability_proxy(DatabaseCapability),
            ),
        )
        cleanup_registry = SessionCleanupRegistry()
        cleanup_registry.register(messages.cleanup_session_data)
        sessions = DatabaseRequestSessionStorage(
            database=site.capability_proxy(DatabaseCapability),
            connection_name="default",
            payload_max_bytes=1024,
            cleanup_registry=cleanup_registry,
        )
        session_data: dict[str, object] = {}
        session_id = create_session_id(now=1.0)
        await messages.error(_request(session_data), "Stored")
        await sessions.save(
            session_id,
            SessionRecord(
                data=dict(session_data),
                created_at=1.0,
                updated_at=1.0,
                expires_at=100.0,
            ),
        )

        assert SESSION_QUEUE_ID_KEY in session_data
        assert await _message_alert_count(db_capability) == 1

        await sessions.delete(session_id)

        assert await _message_alert_count(db_capability) == 0


@pytest.mark.anyio
async def test_database_storage_removes_alert_queue_when_session_expires() -> None:
    settings = _settings({"storage_backend": "database"})
    async with _database_site(
        modules=("wybra.messages", "wybra.sessions"),
    ) as (site, db_capability):
        messages = DefaultMessagesCapability(
            settings,
            DatabaseMessagesStorage(
                settings,
                site.capability_proxy(DatabaseCapability),
            ),
        )
        cleanup_registry = SessionCleanupRegistry()
        cleanup_registry.register(messages.cleanup_session_data)
        sessions = DatabaseRequestSessionStorage(
            database=site.capability_proxy(DatabaseCapability),
            connection_name="default",
            payload_max_bytes=1024,
            cleanup_registry=cleanup_registry,
        )
        session_data: dict[str, object] = {}
        session_id = create_session_id(now=1.0)
        await messages.warning(_request(session_data), "Expired")
        await sessions.save(
            session_id,
            SessionRecord(
                data=dict(session_data),
                created_at=1.0,
                updated_at=1.0,
                expires_at=2.0,
            ),
        )

        assert await _message_alert_count(db_capability) == 1
        assert await sessions.load(session_id, now=3.0) is None

        assert await _message_alert_count(db_capability) == 0


@pytest.mark.anyio
async def test_database_storage_cleanup_removes_expired_alerts() -> None:
    settings = _settings({"storage_backend": "database"})
    async with _database_site() as (site, db_capability):
        storage = DatabaseMessagesStorage(
            settings,
            site.capability_proxy(DatabaseCapability),
        )
        async with tortoise_transaction(
            db_capability, db_capability.database().for_write()
        ) as connection:
            await MessageAlert.create(
                queue_key="queue",
                severity=SUCCESS_ALERT,
                message="Expired",
                created_at=1.0,
                expires_at=2.0,
                using_db=connection,
            )

        assert await _message_alert_count(db_capability) == 1

        await storage.cleanup(now=3.0)

        assert await _message_alert_count(db_capability) == 0


def test_database_storage_exposes_model_and_migration_surface() -> None:
    migration_locations = discover_migration_version_locations("wybra.messages")

    assert discover_model_package("wybra.messages") == "wybra.messages.models"
    assert migration_locations
    assert any(
        path.name == "migrations" and path.joinpath("0001_initial.py").is_file()
        for path in migration_locations
    )


@pytest.mark.anyio
async def test_messages_context_peeks_until_alerts_are_rendered() -> None:
    settings = _settings()
    capability = DefaultMessagesCapability(settings, SessionMessagesStorage(settings))
    site = _site(settings, capability)
    session: dict[str, object] = {}
    request = _request(session)
    request.scope["app"] = site.app

    await capability.success(request, "Saved")

    context = await messages_context(request, TemplateContext())
    repeated_context = await messages_context(request, TemplateContext())

    assert SESSION_ALERTS_KEY in session
    assert bool(context["has_alerts"]) is True
    assert context["messages_enabled"] is True
    assert [alert.message for alert in context["alerts"]] == ["Saved"]
    assert [alert.message for alert in repeated_context["alerts"]] == ["Saved"]
    assert getattr(request.state, REQUEST_ALERTS_RENDERED_ATTRIBUTE) is True

    await capability.acknowledge_alerts(request)

    assert SESSION_ALERTS_KEY not in session


@pytest.mark.anyio
async def test_default_alert_component_escapes_message_text() -> None:
    renderer = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.messages", directory="templates"),
        ),
        include_request_context=False,
        cache_size=0,
    )

    content = await renderer.render_template(
        "components/alerts.html",
        {
            "alerts": (
                AlertRecord.create(
                    ERROR_ALERT,
                    "<strong>Unsafe</strong>",
                    max_message_length=100,
                    created_at=1.0,
                ),
            )
        },
    )

    assert "&lt;strong&gt;Unsafe&lt;/strong&gt;" in content
    assert "<strong>Unsafe</strong>" not in content
    assert 'data-alert-severity="error"' in content
    assert 'aria-label="Page notifications"' in content
    assert 'aria-labelledby="wybra-alert-1-heading"' in content
    assert "Error notification" in content
    assert "wybra-visually-hidden" in content


async def _message_alert_count(
    db_capability: DatabaseCapability,
) -> int:
    async with tortoise_transaction(
        db_capability, db_capability.database().for_write()
    ) as connection:
        return await MessageAlert.all(using_db=connection).count()


@pytest.mark.anyio
async def test_widget_layout_renders_alert_component_when_context_exists() -> None:
    renderer = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
            PackageResourceSource(package="wybra.messages", directory="templates"),
        ),
        include_request_context=False,
        cache_size=0,
    )

    content = await renderer.render_template(
        "layouts/page.html",
        {
            "asset_url": lambda path: f"/static/{path}",
            "has_alerts": True,
            "messages_enabled": True,
            "page_title": "Page",
            "route_name": "page",
            "theme_attribute": "",
            "alerts": (
                AlertRecord.create(
                    SUCCESS_ALERT,
                    "Saved",
                    max_message_length=100,
                    created_at=1.0,
                ),
            ),
        },
    )

    assert "/static/styles/messages.css" in content
    assert "wybra-alert--success" in content


@pytest.mark.anyio
async def test_widget_layout_omits_alert_component_without_context() -> None:
    renderer = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        ),
        include_request_context=False,
        cache_size=0,
    )

    content = await renderer.render_template(
        "layouts/page.html",
        {
            "asset_url": lambda path: f"/static/{path}",
            "page_title": "Page",
            "route_name": "page",
            "theme_attribute": "",
        },
    )

    assert "styles/messages.css" not in content
    assert "wybra-alert" not in content


def test_alerts_validation_target_is_discovered() -> None:
    targets = discover_validation_targets(("wybra.messages",))

    assert "alerts" in targets


def test_validate_alerts_checks_settings_and_resources() -> None:
    settings = SimpleNamespace(
        modules=("wybra.messages",),
        config=ConfigService(
            [MappingConfigSource({"wybra.messages": {}})],
            config_defs=(module_config,),
            discover_module_config=False,
        ),
    )

    result = validate_alerts(settings)

    assert result.is_ok
