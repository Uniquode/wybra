from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_connection
from wybra.messages import MessagesCapability
from wybra.sessions.models import SessionRecordModel
from wybra.sessions.storage import SessionRecord
from wybra.site import get_site
from wybra.testing import (
    RecordingIdentityDelivery,
    RecordingMessages,
    WybraTestClient,
    application_test_config,
    configuration_with_overrides,
    create_test_application,
    create_test_site,
    create_test_user,
    in_memory_database_url,
    memory_session_storage,
    migrated_test_application,
    migrated_test_database,
)


@pytest.mark.anyio
async def test_migrated_test_database_applies_native_migrations() -> None:
    async with migrated_test_database(modules=("wybra.db",)) as database:
        _count, rows = await database.connection().execute_query(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )

    assert {row["name"] for row in rows} >= {
        "sessions_session",
        "tortoise_migrations",
    }


@pytest.mark.anyio
async def test_migrated_test_database_clears_data_but_retains_migrations() -> None:
    async with migrated_test_database(modules=("wybra.db",)) as database:
        await SessionRecordModel.create(
            id="test-session",
            data="{}",
            created_at=1.0,
            updated_at=1.0,
            expires_at=2.0,
        )

        await database.clear()

        assert await SessionRecordModel.all().count() == 0
        _count, rows = await database.connection().execute_query(
            "SELECT COUNT(*) AS count FROM tortoise_migrations"
        )

    assert rows[0]["count"] > 0


@pytest.mark.anyio
async def test_migrated_test_database_exposes_live_database_capability() -> None:
    async with migrated_test_database(modules=("wybra.db",)) as database:
        capability = database.capability()
        assert (
            tortoise_connection(capability, capability.database().default())
            is database.connection()
        )


@pytest.mark.anyio
async def test_create_test_user_creates_a_verified_local_account() -> None:
    async with migrated_test_database(modules=("wybra.db", "wybra.auth")):
        user = await create_test_user(email="person@example.test")

        assert user.email == "person@example.test"
        assert user.is_active is True
        assert user.is_verified is True


@pytest.mark.anyio
async def test_memory_session_storage_preserves_records_for_direct_tests() -> None:
    storage = memory_session_storage()
    record = SessionRecord(
        data={"user_id": "user-1"},
        created_at=1.0,
        updated_at=1.0,
        expires_at=2.0,
    )

    await storage.save("session-1", record)

    assert await storage.load("session-1", now=1.5) == record


def test_in_memory_database_url_uses_tortoise_native_syntax() -> None:
    assert in_memory_database_url() == "sqlite://:memory:"


def test_create_test_site_supports_direct_capability_test_doubles() -> None:
    site = create_test_site({"app": {"modules": ()}})
    messages = RecordingMessages()

    site.provide_capability(MessagesCapability, messages)

    assert site.require_capability(MessagesCapability) is messages


@pytest.mark.anyio
async def test_wybra_test_client_uses_composed_app_and_migrated_tables() -> None:
    app = FastAPI()

    @app.post("/sessions/{session_id}")
    async def create_session(session_id: str, request: Request) -> dict[str, int]:
        database = get_site(request.app).require_capability(DatabaseCapability)
        connection = tortoise_connection(
            database,
            database.database().for_write(),
        )
        await connection.execute_query(
            "INSERT INTO sessions_session "
            "(id, data, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [session_id, "{}", 1.0, 1.0, 2.0],
        )
        _count, rows = await connection.execute_query(
            "SELECT COUNT(*) AS count FROM sessions_session"
        )
        return {"count": int(rows[0]["count"])}

    application = create_test_application(
        application_test_config(modules=("wybra.db",)),
        app=app,
    )
    async with migrated_test_application(application) as test_application:
        response = await test_application.client.post("/sessions/first")
        await test_application.database.clear()
        _count, rows = await test_application.database.connection().execute_query(
            "SELECT COUNT(*) AS count FROM sessions_session"
        )

    assert response.status_code == 200
    assert response.json() == {"count": 1}
    assert rows[0]["count"] == 0


@pytest.mark.anyio
async def test_wybra_test_client_preserves_cookies_and_returns_server_errors() -> None:
    app = FastAPI()

    @app.get("/cookie")
    async def set_cookie() -> PlainTextResponse:
        response = PlainTextResponse("set")
        response.set_cookie("example", "value")
        return response

    @app.get("/read-cookie")
    async def read_cookie(request: Request) -> PlainTextResponse:
        return PlainTextResponse(request.cookies.get("example", ""))

    @app.get("/error")
    async def error() -> None:
        raise RuntimeError("test error")

    async with WybraTestClient(app) as client:
        await client.get("/cookie")
        cookie_response = await client.get("/read-cookie")

    async with WybraTestClient(app, raise_server_exceptions=False) as client:
        error_response = await client.get("/error")

    assert cookie_response.text == "value"
    assert error_response.status_code == 500


@pytest.mark.anyio
async def test_ad_hoc_wybra_test_client_rejects_in_memory_database() -> None:
    app = create_test_application(
        application_test_config(modules=("wybra.db",)),
    )
    async with migrated_test_application(app) as application:
        with pytest.raises(
            RuntimeError,
            match="Ad-hoc WybraTestClient requests cannot use an in-memory database",
        ):
            WybraTestClient(app).get("/")

        _count, rows = await application.database.connection().execute_query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'sessions_session'"
        )

    assert [row["name"] for row in rows] == ["sessions_session"]


def test_configuration_with_overrides_does_not_mutate_input() -> None:
    values = {"app": {"modules": ("wybra.db",), "debug": False}}

    configured = configuration_with_overrides(
        values,
        {"app": {"debug": True}, "auth": {"totp_mode": "optional"}},
    )

    assert configured == {
        "app": {"modules": ("wybra.db",), "debug": True},
        "auth": {"totp_mode": "optional"},
    }
    assert values == {"app": {"modules": ("wybra.db",), "debug": False}}


@dataclass(frozen=True, slots=True)
class _DeliveryUser:
    id: str = "user-id"


@pytest.mark.anyio
async def test_recording_identity_delivery_captures_delivery_kind_and_token() -> None:
    delivery = RecordingIdentityDelivery()
    user = _DeliveryUser()

    await delivery.send_verification_token(user, "verify-token")  # type: ignore[arg-type]
    await delivery.send_reset_password_token(user, "reset-token")  # type: ignore[arg-type]

    assert [(record.kind, record.token) for record in delivery.deliveries] == [
        ("verification", "verify-token"),
        ("reset_password", "reset-token"),
    ]


@pytest.mark.anyio
async def test_recording_messages_exposes_alerts_and_consumes_them() -> None:
    app = FastAPI()
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )
    messages = RecordingMessages()

    await messages.success(request, "Saved")
    alerts = await messages.consume_alerts(request)

    assert [alert.message for alert in alerts] == ["Saved"]
    assert await messages.peek_alerts(request) == ()
