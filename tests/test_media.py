from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from fastapi import FastAPI

import wybra.media as media_module
import wybra.media.capabilities as media_capabilities
import wybra.media.models as media_models
from support_database import sqlite_file_url
from wybra.auth import models as auth_models  # noqa: F401
from wybra.config import ConfigService, MappingConfigSource
from wybra.core import InputValidationError
from wybra.db import DatabaseCapability
from wybra.db.capabilities import WybraDatabaseCapability, tortoise_transaction
from wybra.db.surfaces import discover_model_package
from wybra.media import (
    FilesystemMediaCapability,
    MediaCapability,
    MediaCapabilityError,
    MediaError,
    MediaInputError,
    MediaNotFoundError,
    MediaSettings,
    MediaStorageOperationError,
    MediaStorageReadinessError,
)
from wybra.media.models import MediaItem
from wybra.media.persistence import (
    MediaCatalogueRepository,
    TortoiseMediaCatalogueRepository,
)
from wybra.media.validation import validate_media
from wybra.site import Site, SiteCapabilityError, start
from wybra.testing import WybraTestClient, create_test_database


class FakeUpload:
    def __init__(self, chunks: tuple[bytes, ...], content_type: str | None) -> None:
        self._chunks = list(chunks)
        self.content_type = content_type

    async def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _config(
    tmp_path: Path,
    media_config: dict[str, object] | None = None,
) -> ConfigService:
    return ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "project_root": tmp_path,
                        "modules": ("wybra.media",),
                    },
                    "wybra.media": media_config or {},
                }
            )
        ]
    )


class UnusedMediaCatalogueRepository:
    async def create_item(self, **_kwargs: object) -> MediaItem:
        raise AssertionError("Media catalogue should not be used.")

    async def get_item(self, _media_id: uuid.UUID) -> MediaItem | None:
        raise AssertionError("Media catalogue should not be used.")

    async def get_item_by_resource_key(self, _resource_key: str) -> MediaItem | None:
        raise AssertionError("Media catalogue should not be used.")

    async def assign_resource_key(
        self,
        _media_id: uuid.UUID,
        _resource_key: str,
    ) -> None:
        raise AssertionError("Media catalogue should not be used.")


async def _site_with_media_database(tmp_path: Path) -> Site:
    site = Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource({"app": {"modules": ()}})],
            discover_module_config=False,
        ),
    )
    database = await create_test_database(
        database_url=sqlite_file_url(tmp_path / "media.sqlite3"),
        modules=("wybra.media",),
    )
    site.provide_capability(
        DatabaseCapability,
        WybraDatabaseCapability(database),
    )
    return site


def _capability(
    tmp_path: Path,
    *,
    url_mode: str = "storage-key",
    catalogue: MediaCatalogueRepository | None = None,
) -> FilesystemMediaCapability:
    return FilesystemMediaCapability(
        MediaSettings(root=tmp_path, url_mode=url_mode),
        catalogue=catalogue or UnusedMediaCatalogueRepository(),
    )


@pytest.fixture
async def database_capability_factory() -> AsyncIterator[
    Callable[..., Awaitable[FilesystemMediaCapability]]
]:
    sites: list[Site] = []

    async def factory(
        tmp_path: Path,
        *,
        url_mode: str = "storage-key",
    ) -> FilesystemMediaCapability:
        site = await _site_with_media_database(tmp_path)
        sites.append(site)
        return _capability(
            tmp_path,
            url_mode=url_mode,
            catalogue=TortoiseMediaCatalogueRepository(
                site.capability_proxy(DatabaseCapability)
            ),
        )

    try:
        yield factory
    finally:
        while sites:
            await sites.pop().close()


async def _asgi_get_status(
    app: FastAPI,
    path: str,
    *,
    raise_server_exceptions: bool = True,
) -> int:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    response_status = 500
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        nonlocal response_status
        if message["type"] == "http.response.start":
            response_status = int(message["status"])

    try:
        await app(scope, receive, send)
    except Exception:
        if raise_server_exceptions:
            raise
        return 500
    return response_status


@pytest.mark.parametrize(
    ("media_config", "root_name", "mount_path", "serve", "url_mode"),
    (
        ({}, "media", "/media", True, "storage-key"),
        (
            {
                "root": "uploads",
                "mount_path": "uploads",
                "serve": False,
                "url_mode": "id",
            },
            "uploads",
            "/uploads",
            False,
            "id",
        ),
    ),
    ids=("defaults", "configured"),
)
def test_media_settings_resolve_values(
    tmp_path: Path,
    media_config: dict[str, object],
    root_name: str,
    mount_path: str,
    serve: bool,
    url_mode: str,
) -> None:
    settings = MediaSettings.load_settings(_config(tmp_path, media_config))

    assert settings.root == tmp_path / root_name
    assert settings.mount_path == mount_path
    assert settings.serve is serve
    assert settings.url_mode == url_mode


def test_media_model_surface_exposes_tortoise_models() -> None:
    assert discover_model_package("wybra.media") == "wybra.media.models"


def test_media_exceptions_inherit_from_media_error() -> None:
    for exception_type in (
        MediaCapabilityError,
        MediaInputError,
        MediaNotFoundError,
        MediaStorageOperationError,
        MediaStorageReadinessError,
    ):
        assert issubclass(exception_type, MediaError)


def test_media_capability_resolves_safe_key_paths(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    assert (
        capability.path_for_key("profiles/avatar.png")
        == (tmp_path / "profiles" / "avatar.png").resolve()
    )
    assert capability.url_for_key("profiles/avatar.png") == "/media/profiles/avatar.png"


@pytest.mark.parametrize("key", ["../secret.txt", "/tmp/secret.txt", "profiles/../x"])
def test_media_capability_rejects_unsafe_paths(tmp_path: Path, key: str) -> None:
    capability = _capability(tmp_path)

    with pytest.raises(InputValidationError) as excinfo:
        capability.path_for_key(key)

    assert isinstance(excinfo.value, MediaInputError)


@pytest.mark.parametrize(
    ("category", "message"),
    (
        (" ", "Media category must not be blank."),
        ("profile/avatar", "Media category must not contain path separators."),
    ),
)
@pytest.mark.anyio
async def test_media_capability_rejects_invalid_categories(
    tmp_path: Path,
    category: str,
    message: str,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    with pytest.raises(InputValidationError, match=re.escape(message)) as excinfo:
        await capability.register(category=category, storage_key="profiles/avatar.png")

    assert isinstance(excinfo.value, MediaInputError)


@pytest.mark.anyio
async def test_media_capability_rejects_negative_media_size(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    with pytest.raises(
        InputValidationError, match="Media size must not be negative"
    ) as excinfo:
        await capability.register(
            category="profile",
            storage_key="profiles/avatar.png",
            size=-1,
        )
    assert isinstance(excinfo.value, MediaInputError)


@pytest.mark.parametrize("resource_key", (42, " "))
@pytest.mark.anyio
async def test_media_capability_rejects_invalid_resource_keys(
    tmp_path: Path,
    resource_key: object,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    with pytest.raises(InputValidationError) as excinfo:
        await capability.register(
            category="profile",
            storage_key="profiles/avatar.png",
            resource_key=resource_key,  # type: ignore[arg-type]
        )

    assert "Media resource key" in str(excinfo.value)
    assert isinstance(excinfo.value, MediaInputError)


def test_media_capability_validates_writable_root(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    capability.validate_writable()


def test_media_capability_rejects_missing_writable_root(tmp_path: Path) -> None:
    capability = FilesystemMediaCapability(
        MediaSettings(root=tmp_path / "missing"),
        catalogue=UnusedMediaCatalogueRepository(),
    )

    with pytest.raises(MediaStorageReadinessError, match="does not exist") as excinfo:
        capability.validate_writable()
    assert isinstance(excinfo.value, MediaStorageReadinessError)


def test_media_capability_reports_writable_root_probe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _capability(tmp_path)

    def fail_touch(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr(Path, "touch", fail_touch)

    with pytest.raises(MediaStorageOperationError, match="not writable") as excinfo:
        capability.validate_writable()
    assert isinstance(excinfo.value, MediaStorageOperationError)


@pytest.mark.anyio
async def test_media_capability_registers_catalogue_item_and_resolves_by_id(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        content_type="image/png",
        size=123,
    )

    assert isinstance(item, MediaItem)
    assert item.category == "profile"
    assert (
        await capability.path_for(item.id)
        == (tmp_path / "profile" / "ab" / "cd" / "user.png").resolve()
    )
    assert await capability.url_for(item.id) == "/media/profile/ab/cd/user.png"


@pytest.mark.anyio
async def test_media_item_save_refreshes_modified_timestamp_for_partial_updates(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = await database_capability_factory(tmp_path)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        content_type="image/png",
        size=123,
    )
    updated_timestamp = item.modified_at + 10.0
    monkeypatch.setattr(media_models.time, "time", lambda: updated_timestamp)

    item.content_type = "image/jpeg"
    database = await capability.catalogue.database.require()
    async with tortoise_transaction(
        database,
        database.database().for_write(),
    ) as connection:
        await item.save(using_db=connection, update_fields=("content_type",))
        stored = await MediaItem.get(id=item.id, using_db=connection)

    assert stored.content_type == "image/jpeg"
    assert stored.modified_at == updated_timestamp


@pytest.mark.anyio
async def test_media_capability_registers_and_resolves_resource_key(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        resource_key="country-codes",
    )

    resolved = await capability.get_by_resource_key("country-codes")

    assert resolved.id == item.id


@pytest.mark.anyio
async def test_media_capability_store_accepts_resource_key(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    item = await capability.store(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        upload=FakeUpload((b"ava", b"tar"), "image/png"),
        resource_key="profile-picture",
    )

    resolved = await capability.get_by_resource_key("profile-picture")

    assert resolved.id == item.id


@pytest.mark.anyio
async def test_media_capability_rejects_resource_key_reassignment(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    first = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/first.png",
    )
    second = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/second.png",
    )

    await capability.assign_resource_key(
        first.id,
        "default-avatar",
    )
    await capability.assign_resource_key(
        first.id,
        "default-avatar",
    )

    with pytest.raises(MediaInputError, match="Media resource key is already assigned"):
        await capability.assign_resource_key(
            second.id,
            "default-avatar",
        )

    assert (await capability.get_by_resource_key("default-avatar")).id == first.id


@pytest.mark.anyio
async def test_media_capability_rejects_duplicate_resource_key_on_register(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    first = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/first.png",
        resource_key="default-avatar",
    )

    with pytest.raises(MediaInputError, match="Media resource key is already assigned"):
        await capability.register(
            category="profile",
            storage_key="profile/ab/cd/second.png",
            resource_key="default-avatar",
        )

    assert (await capability.get_by_resource_key("default-avatar")).id == first.id


@pytest.mark.anyio
async def test_media_capability_stores_upload_and_registers_catalogue_item(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    item = await capability.store(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        upload=FakeUpload((b"ava", b"tar"), "image/png"),
        chunk_size=3,
    )

    assert (tmp_path / "profile" / "ab" / "cd" / "user.png").read_bytes() == b"avatar"
    assert item.category == "profile"
    assert item.storage_key == "profile/ab/cd/user.png"
    assert item.content_type == "image/png"
    assert item.size == 6
    tmp_root = tmp_path / ".tmp"

    if tmp_root.exists():
        assert not any(tmp_root.iterdir())


@pytest.mark.anyio
async def test_media_capability_reports_upload_write_failures_as_storage_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)
    media_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    temp_destination = tmp_path / ".tmp" / f"{media_uuid.hex}.user.png.tmp"

    class FailingOutput:
        async def __aenter__(self) -> FailingOutput:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def write(self, _chunk: bytes) -> None:
            temp_destination.write_bytes(b"partial")
            raise OSError("disk unavailable")

    async def fail_open_file(*args: object, **kwargs: object) -> object:
        return FailingOutput()

    monkeypatch.setattr(media_capabilities.uuid, "uuid4", lambda: media_uuid)
    monkeypatch.setattr(anyio, "open_file", fail_open_file)

    with pytest.raises(
        MediaStorageOperationError,
        match="Media storage operation failed",
    ) as excinfo:
        await capability.store(
            category="profile",
            storage_key="profile/ab/cd/user.png",
            upload=FakeUpload((b"avatar",), "image/png"),
        )

    assert isinstance(excinfo.value, MediaStorageOperationError)
    assert not (tmp_path / "profile" / "ab" / "cd" / "user.png").exists()
    assert not temp_destination.exists()
    tmp_root = tmp_path / ".tmp"
    if tmp_root.exists():
        assert not any(tmp_root.iterdir())


@pytest.mark.anyio
async def test_media_capability_cleans_temp_file_for_upload_read_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)
    media_uuid = uuid.UUID("87654321-4321-8765-4321-876543218765")
    temp_destination = tmp_path / ".tmp" / f"{media_uuid.hex}.user.png.tmp"

    class FailingReadUpload:
        content_type = "image/png"

        def __init__(self) -> None:
            self._read_count = 0

        async def read(self, _size: int = -1) -> bytes:
            self._read_count += 1
            if self._read_count == 1:
                return b"partial"
            raise RuntimeError("upload stream failed")

    monkeypatch.setattr(media_capabilities.uuid, "uuid4", lambda: media_uuid)

    with pytest.raises(RuntimeError, match="upload stream failed"):
        await capability.store(
            category="profile",
            storage_key="profile/ab/cd/user.png",
            upload=FailingReadUpload(),
        )

    assert not (tmp_path / "profile" / "ab" / "cd" / "user.png").exists()
    assert not temp_destination.exists()
    tmp_root = tmp_path / ".tmp"
    if tmp_root.exists():
        assert not any(tmp_root.iterdir())


@pytest.mark.anyio
async def test_media_capability_removes_destination_if_register_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _capability(tmp_path)
    destination = tmp_path / "profile" / "ab" / "cd" / "user.png"

    async def failing_register(
        self: FilesystemMediaCapability,
        *args: object,
        **kwargs: object,
    ) -> MediaItem:
        raise RuntimeError("register failed")

    monkeypatch.setattr(FilesystemMediaCapability, "register", failing_register)

    with pytest.raises(RuntimeError, match="register failed"):
        await capability.store(
            category="profile",
            storage_key="profile/ab/cd/user.png",
            upload=FakeUpload((b"avatar",), "image/png"),
        )

    assert not destination.exists()
    tmp_root = tmp_path / ".tmp"
    if tmp_root.exists():
        assert not any(tmp_root.iterdir())


@pytest.mark.anyio
async def test_media_capability_rejects_invalid_upload_chunk_size(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)

    with pytest.raises(InputValidationError, match="chunk size") as excinfo:
        await capability.store(
            category="profile",
            storage_key="profile/ab/cd/user.png",
            upload=FakeUpload((b"avatar",), "image/png"),
            chunk_size=0,
        )
    assert isinstance(excinfo.value, MediaInputError)


@pytest.mark.anyio
async def test_media_capability_resolves_id_url_mode(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path, url_mode="id")

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        size=123,
    )

    assert await capability.url_for(item.id) == f"/media/items/{item.id}"


@pytest.mark.anyio
async def test_media_capability_rejects_unknown_resource_key(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    capability = await database_capability_factory(tmp_path)

    with pytest.raises(MediaNotFoundError) as excinfo:
        await capability.get_by_resource_key("missing")
    assert isinstance(excinfo.value, MediaNotFoundError)


@pytest.mark.anyio
async def test_media_item_route_returns_not_found_for_missing_media(
    tmp_path: Path,
    database_capability_factory: Callable[..., Awaitable[FilesystemMediaCapability]],
) -> None:
    app = FastAPI()
    capability = await database_capability_factory(tmp_path, url_mode="id")
    site = Site(app=app, config=_config(tmp_path))

    media_module._register_media_item_route(site, capability)

    assert await _asgi_get_status(app, f"/media/items/{uuid.uuid4()}") == 404


@pytest.mark.anyio
async def test_media_item_route_returns_not_found_for_missing_file(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    site = Site(app=app, config=_config(tmp_path))
    media_id = uuid.uuid4()

    class MissingFileMediaCapability:
        root = tmp_path
        mount_path = "/media"
        serve = True
        url_mode = "id"

        async def get(self, _media_id: uuid.UUID) -> MediaItem:
            return MediaItem(
                id=media_id,
                category="profile",
                storage_key="profile/ab/cd/user.png",
                content_type="image/png",
                size=123,
            )

        async def path_for(self, _media_id: uuid.UUID) -> Path:
            raise FileNotFoundError("profile/ab/cd/user.png")

    media_module._register_media_item_route(site, MissingFileMediaCapability())

    response = WybraTestClient(app).get(f"/media/items/{media_id}")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_media_item_route_does_not_mask_storage_failures(tmp_path: Path) -> None:
    app = FastAPI()
    site = Site(app=app, config=_config(tmp_path))

    class FailingMediaCapability:
        root = tmp_path
        mount_path = "/media"
        serve = True
        url_mode = "id"

        async def get(self, _media_id: uuid.UUID):
            raise MediaStorageReadinessError("storage unavailable")

        async def path_for(self, _media_id: uuid.UUID) -> Path:
            raise AssertionError("path_for should not be called")

    media_module._register_media_item_route(site, FailingMediaCapability())

    response = WybraTestClient(app, raise_server_exceptions=False).get(
        f"/media/items/{uuid.uuid4()}"
    )

    assert response.status_code == 500


def test_validate_media_reports_missing_root(tmp_path: Path) -> None:
    class Settings:
        media_root = tmp_path / "missing"
        media_mount_path = "/media"
        media_serve = True
        media_url_mode = "storage-key"

    result = validate_media(Settings())

    assert result.is_ok is False
    assert result.errors == (f"Media root must exist: {tmp_path / 'missing'}",)


def test_validate_media_accepts_existing_root(tmp_path: Path) -> None:
    class Settings:
        media_root = tmp_path
        media_mount_path = "/media"
        media_serve = True
        media_url_mode = "storage-key"

    result = validate_media(Settings())

    assert result.is_ok is True


@pytest.mark.anyio
async def test_media_setup_registers_capability_and_serves_files(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "avatar.txt").write_text("avatar", encoding="utf-8")
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "project_root": tmp_path,
                    "modules": ("wybra.media", "wybra.db"),
                    "database_url": sqlite_file_url(tmp_path / "app.sqlite3"),
                },
                "wybra.media": {"root": "media", "mount_path": "/media"},
            }
        ),
    )
    try:
        assert (
            site.require_capability(MediaCapability).path_for_key("avatar.txt")
            == (media_root / "avatar.txt").resolve()
        )
        assert WybraTestClient(app).get("/media/avatar.txt").text == "avatar"
    finally:
        await site.close()


@pytest.mark.anyio
async def test_media_setup_registers_capability_before_database_exists(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {
                    "project_root": tmp_path,
                    "modules": ("wybra.media", "wybra.db"),
                    "database_url": sqlite_file_url(tmp_path / "app.sqlite3"),
                },
                "wybra.media": {"root": "media", "mount_path": "/media"},
            }
        ),
    )
    try:
        assert site.has_capability(MediaCapability) is True
        assert site.has_capability(DatabaseCapability) is True
    finally:
        await site.close()


@pytest.mark.anyio
async def test_media_post_setup_requires_database_capability(tmp_path: Path) -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "project_root": tmp_path,
                        "modules": ("wybra.media",),
                    },
                    "wybra.media": {"root": "media", "mount_path": "/media"},
                }
            ),
        )


@pytest.mark.anyio
async def test_media_setup_skips_serving_when_disabled(tmp_path: Path) -> None:
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "project_root": tmp_path,
                    "modules": ("wybra.media", "wybra.db"),
                    "database_url": sqlite_file_url(tmp_path / "app.sqlite3"),
                },
                "wybra.media": {"serve": False},
            }
        ),
    )
    try:
        assert WybraTestClient(app).get("/media/avatar.txt").status_code == 404
    finally:
        await site.close()
