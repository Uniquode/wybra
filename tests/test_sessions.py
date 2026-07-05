from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.exceptions import ConfigurationError
from wybra.db import DatabaseCapability, SqlAlchemyDatabaseCapability
from wybra.db.persistence import close_database, create_database
from wybra.db.surfaces import (
    discover_migration_version_locations,
    metadata_from_model_package,
    migration_version_locations_from_modules,
    model_packages_from_modules,
)
from wybra.services.crypto import (
    ENV_WYBRA_SECRET_KEY_CURRENT,
    SecretEnvelopeService,
    generate_secret_key_entry,
)
from wybra.sessions import (
    CookieSessionStorage,
    DatabaseSessionStorage,
    FileSessionStorage,
    MemorySessionStorage,
    RequestSession,
    SessionIdentifierError,
    SessionMiddlewareContext,
    SessionRecord,
    SessionRecordModel,
    SessionsConfigurationError,
    SessionsSettings,
    SessionStorage,
    SessionStorageBackend,
    create_session_id,
    module_config,
    setup_core_sessions,
    validate_session_id,
    validate_sessions,
)
from wybra.sessions.middleware import SESSION_CLEANUP_INTERVAL_SECONDS
from wybra.sessions.setup import session_storage_from_site
from wybra.sessions.storage import CacheSessionStorage, SessionStorageError
from wybra.site import Site, SiteCapabilityError, start, start_site


def _config(
    *,
    app: dict[str, object] | None = None,
    sessions: dict[str, object] | None = None,
    environ: dict[str, str] | None = None,
) -> ConfigService:
    return ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {"deployment_environment": "local", **(app or {})},
                    "wybra.sessions": sessions or {},
                }
            )
        ],
        config_defs=(RUNTIME_CONFIG_DEF, module_config),
        environ={} if environ is None else environ,
        discover_module_config=False,
    )


def _settings(
    values: dict[str, object] | None = None,
    *,
    app: dict[str, object] | None = None,
    environ: dict[str, str] | None = None,
) -> SessionsSettings:
    return SessionsSettings.load_settings(
        _config(app=app, sessions=values, environ=environ)
    )


def _record(
    *,
    data: dict[str, object] | None = None,
    created_at: float = 1.0,
    updated_at: float = 1.0,
    expires_at: float = 60.0,
) -> SessionRecord:
    return SessionRecord(
        data={} if data is None else data,
        created_at=created_at,
        updated_at=updated_at,
        expires_at=expires_at,
    )


def test_sessions_settings_defaults_to_cookie_for_local_deployments() -> None:
    settings = _settings()

    assert settings.resolved_storage_backend is SessionStorageBackend.COOKIE
    assert settings.resolved_cookie_secure is False
    assert settings.resolved_lifetime_seconds == 14 * 24 * 60 * 60


def test_sessions_settings_requires_explicit_backend_outside_local() -> None:
    with pytest.raises(ConfigurationError, match="storage_backend"):
        _settings(app={"deployment_environment": "production"})


def test_sessions_settings_rejects_invalid_backend() -> None:
    with pytest.raises(ConfigSourceError, match="storage_backend"):
        _settings({"storage_backend": "unknown"})


def test_sessions_settings_rejects_same_site_none_without_secure() -> None:
    with pytest.raises(ConfigurationError, match="cookie_secure"):
        _settings({"cookie_same_site": "none"})


def test_sessions_settings_resolves_file_directory_against_project_root(
    tmp_path: Path,
) -> None:
    settings = _settings(
        {"storage_backend": "file", "file_directory": "runtime/sessions"},
        app={"project_root": tmp_path},
    )

    assert settings.resolved_file_directory == (tmp_path / "runtime/sessions")


def test_session_ids_are_safe_validated_and_timestamp_ordered() -> None:
    earlier = create_session_id(now=10.0)
    later = create_session_id(now=11.0)

    assert validate_session_id(earlier) == earlier
    assert validate_session_id(later) == later
    assert earlier < later
    assert "/" not in earlier
    assert "\\" not in earlier

    with pytest.raises(SessionIdentifierError):
        validate_session_id("../unsafe")


def test_request_session_tracks_mutation_and_clear_state() -> None:
    session = RequestSession({"existing": "value"}, session_id="session-id")

    session["new"] = "saved"
    assert session.modified is True
    assert session.accessed is True
    assert session.cleared is False

    session.clear()
    assert dict(session) == {}
    assert session.modified is True
    assert session.cleared is True


@pytest.mark.anyio
async def test_memory_storage_saves_copies_and_expires_records() -> None:
    storage = MemorySessionStorage(payload_max_bytes=1024)
    record = _record(data={"value": "saved"}, expires_at=5.0)

    await storage.save("session", record)
    loaded = await storage.load("session", now=2.0)
    expired = await storage.load("session", now=6.0)

    assert loaded == record
    assert loaded is not record
    assert expired is None


@pytest.mark.anyio
async def test_storage_rejects_oversized_payloads() -> None:
    storage = MemorySessionStorage(payload_max_bytes=10)

    with pytest.raises(SessionStorageError, match="payload exceeds"):
        await storage.save("session", _record(data={"value": "too large"}))


@pytest.mark.anyio
async def test_file_storage_writes_loads_deletes_and_cleans_expired_records(
    tmp_path: Path,
) -> None:
    storage = FileSessionStorage(directory=tmp_path, payload_max_bytes=1024)
    active_id = create_session_id(now=1.0)
    expired_id = create_session_id(now=2.0)

    await storage.save(active_id, _record(data={"value": "active"}, expires_at=50.0))
    await storage.save(expired_id, _record(data={"value": "old"}, expires_at=2.0))
    await storage.cleanup(now=10.0)

    assert await storage.load(active_id, now=10.0) == _record(
        data={"value": "active"},
        expires_at=50.0,
    )
    assert await storage.load(expired_id, now=10.0) is None
    assert (tmp_path / f"{active_id}.json").is_file()
    assert not (tmp_path / f"{expired_id}.json").exists()

    await storage.delete(active_id)
    assert await storage.load(active_id, now=10.0) is None


def test_cookie_storage_encrypts_and_validates_payloads() -> None:
    storage = CookieSessionStorage(
        service=SecretEnvelopeService.for_testing(),
        payload_max_bytes=1024,
        cookie_payload_max_bytes=4096,
    )
    record = _record(data={"value": "cookie"}, expires_at=20.0)

    cookie_value = storage.dump_cookie("session", record)
    loaded = storage.load_cookie(cookie_value, now=5.0)

    assert loaded == ("session", record)
    assert storage.load_cookie("not-an-envelope", now=5.0) is None
    assert storage.load_cookie(cookie_value, now=25.0) is None


@pytest.mark.anyio
async def test_cache_storage_supports_memory_url() -> None:
    storage = CacheSessionStorage(
        url="memory://sessions",
        key_prefix="test:",
        payload_max_bytes=1024,
    )

    await storage.save("session", _record(data={"value": "cached"}))

    assert await storage.load("session", now=2.0) == _record(data={"value": "cached"})

    await storage.delete("session")
    assert await storage.load("session", now=2.0) is None


@pytest.mark.anyio
async def test_database_storage_persists_session_records() -> None:
    database = create_database("sqlite+aiosqlite:///:memory:")
    capability = SqlAlchemyDatabaseCapability.from_connections({"default": database})
    app = FastAPI()
    site = Site(app=app, config=ConfigService([], discover_module_config=False))
    site.provide_capability(DatabaseCapability, capability)
    storage = DatabaseSessionStorage(
        database=site.capability_proxy(DatabaseCapability),
        connection_name="default",
        payload_max_bytes=1024,
    )

    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(SessionRecordModel.__table__.create)

        await storage.save("session", _record(data={"value": "database"}))
        assert await storage.load("session", now=2.0) == _record(
            data={"value": "database"}
        )

        await storage.save(
            "session",
            _record(data={"value": "updated"}, updated_at=3.0, expires_at=80.0),
        )
        assert await storage.load("session", now=4.0) == _record(
            data={"value": "updated"},
            updated_at=3.0,
            expires_at=80.0,
        )

        await storage.delete("session")
        assert await storage.load("session", now=2.0) is None
    finally:
        await close_database(database)


def test_core_model_and_migration_surfaces_include_sessions() -> None:
    metadata = metadata_from_model_package("wybra.sessions.models")
    model_packages = model_packages_from_modules(())
    migration_locations = migration_version_locations_from_modules(())
    discovered_locations = discover_migration_version_locations("wybra.sessions")

    assert "sessions_session" in metadata.tables
    assert "wybra.sessions.models" in model_packages
    assert discovered_locations
    assert discovered_locations[0] in migration_locations


def test_sessions_validation_target_loads_default_settings() -> None:
    settings = SimpleNamespace(
        config=_config(),
        deployment_environment="local",
    )

    result = validate_sessions(settings)

    assert result.is_ok
    assert any("storage_backend=cookie" in check.description for check in result.checks)


@pytest.mark.anyio
async def test_start_registers_core_session_storage_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
        environ={},
    )

    try:
        assert site.has_capability(SessionStorage) is True
        assert isinstance(session_storage_from_site(site), CookieSessionStorage)
    finally:
        await site.close()


def test_session_middleware_persists_request_session_between_requests() -> None:
    app = FastAPI(
        lifespan=start_site(
            config_source=MappingConfigSource({"app": {"modules": ()}}),
            environ={},
        )
    )

    @app.get("/set")
    async def set_session(request: Request) -> dict[str, str]:
        request.session["value"] = "saved"
        return {"value": request.session["value"]}

    @app.get("/get")
    async def get_session(request: Request) -> dict[str, object]:
        return {"value": request.session.get("value")}

    with TestClient(app) as client:
        set_response = client.get("/set")
        get_response = client.get("/get")

    assert set_response.status_code == 200
    assert get_response.json() == {"value": "saved"}
    assert "wybra_session" in set_response.cookies


def test_session_middleware_clears_session_cookie() -> None:
    app = FastAPI(
        lifespan=start_site(
            config_source=MappingConfigSource({"app": {"modules": ()}}),
            environ={},
        )
    )

    @app.get("/set")
    async def set_session(request: Request) -> dict[str, bool]:
        request.session["value"] = "saved"
        return {"ok": True}

    @app.get("/clear")
    async def clear_session(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/get")
    async def get_session(request: Request) -> dict[str, object]:
        return {"value": request.session.get("value")}

    with TestClient(app) as client:
        client.get("/set")
        clear_response = client.get("/clear")
        get_response = client.get("/get")

    assert clear_response.status_code == 200
    assert get_response.json() == {"value": None}


@pytest.mark.anyio
async def test_session_finalisation_skips_unchanged_sessions() -> None:
    class CountingStorage(MemorySessionStorage):
        def __init__(self) -> None:
            super().__init__(payload_max_bytes=1024)
            self.save_count = 0

        async def save(self, session_id: str, record: SessionRecord) -> None:
            self.save_count += 1
            await super().save(session_id, record)

    storage = CountingStorage()
    context = SessionMiddlewareContext(settings=_settings(), storage=storage)
    session = RequestSession(
        data={"value": "loaded"},
        session_id=create_session_id(now=1.0),
        created_at=1.0,
        expires_at=100.0,
    )

    await context.finalise_response(Response(), session, now=2.0)

    assert storage.save_count == 0


@pytest.mark.anyio
async def test_session_cleanup_runs_at_most_once_per_interval() -> None:
    class CountingStorage(MemorySessionStorage):
        def __init__(self) -> None:
            super().__init__(payload_max_bytes=1024)
            self.cleanup_count = 0

        async def cleanup(self, *, now: float) -> None:
            self.cleanup_count += 1
            await super().cleanup(now=now)

    storage = CountingStorage()
    context = SessionMiddlewareContext(settings=_settings(), storage=storage)

    await context.cleanup_expired(now=10.0)
    await context.cleanup_expired(now=20.0)
    await context.cleanup_expired(now=10.0 + SESSION_CLEANUP_INTERVAL_SECONDS)

    assert storage.cleanup_count == 2


def test_cookie_session_backend_round_trips_through_middleware() -> None:
    app = FastAPI(
        lifespan=start_site(
            config_source=MappingConfigSource(
                {
                    "app": {"modules": ()},
                    "wybra.sessions": {"storage_backend": "cookie"},
                }
            ),
            environ={
                ENV_WYBRA_SECRET_KEY_CURRENT: generate_secret_key_entry(
                    version="current"
                )
            },
        )
    )

    @app.get("/set")
    async def set_session(request: Request) -> dict[str, str]:
        request.session["value"] = "cookie"
        return {"value": request.session["value"]}

    @app.get("/get")
    async def get_session(request: Request) -> dict[str, object]:
        return {"value": request.session.get("value")}

    with TestClient(app) as client:
        response = client.get("/set")
        repeated = client.get("/get")

    assert response.status_code == 200
    assert repeated.json() == {"value": "cookie"}


@pytest.mark.anyio
async def test_starlette_session_middleware_is_rejected() -> None:
    fake_session_middleware = type("SessionMiddleware", (), {})
    fake_session_middleware.__module__ = "starlette.middleware.sessions"
    app = FastAPI()
    app.add_middleware(fake_session_middleware)

    with pytest.raises(SessionsConfigurationError, match="Starlette"):
        await start(
            app,
            config_source=MappingConfigSource({"app": {"modules": ()}}),
            environ={},
        )


@pytest.mark.anyio
async def test_custom_session_storage_can_be_registered_by_module_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "custom_sessions.py").write_text(
        "\n".join(
            (
                "from wybra.sessions import MemorySessionStorage, SessionStorage",
                "STORAGE = MemorySessionStorage(payload_max_bytes=1024)",
                "async def setup_site(site):",
                "    site.provide_capability(SessionStorage, STORAGE)",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("custom_sessions",)}}),
        environ={},
    )

    try:
        custom_module = importlib.import_module("custom_sessions")
        assert session_storage_from_site(site) is custom_module.STORAGE
    finally:
        await site.close()


@pytest.mark.anyio
async def test_invalid_custom_session_storage_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "bad_sessions.py").write_text(
        "\n".join(
            (
                "from wybra.sessions import SessionStorage",
                "async def setup_site(site):",
                "    site.provide_capability(SessionStorage, object())",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(SiteCapabilityError, match="invalid type"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource({"app": {"modules": ("bad_sessions",)}}),
            environ={},
        )


@pytest.mark.anyio
async def test_non_local_startup_requires_explicit_session_backend() -> None:
    with pytest.raises(ConfigurationError, match="storage_backend"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": (), "deployment_environment": "production"}}
            ),
            environ={},
        )


@pytest.mark.anyio
async def test_setup_core_sessions_is_idempotent() -> None:
    site = Site(
        app=FastAPI(),
        config=_config(),
    )

    await setup_core_sessions(site)
    first_storage = session_storage_from_site(site)
    await setup_core_sessions(site)

    assert session_storage_from_site(site) is first_storage
