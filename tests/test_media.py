from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import wybra.media as media_module
import wybra.media.capabilities as media_capabilities
from wybra.auth import models as auth_models  # noqa: F401
from wybra.config import ConfigService, MappingConfigSource
from wybra.core import InputValidationError
from wybra.db import DatabaseCapability, SqlAlchemyDatabaseCapability
from wybra.db.models import metadata
from wybra.db.persistence import create_database
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
from wybra.media.validation import validate_media
from wybra.site import Site, SiteCapabilityError, start

_CREATED_SITES: list[Site] = []


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


def _site_with_media_database(tmp_path: Path) -> Site:
    site = Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource({"app": {"modules": ()}})],
            discover_module_config=False,
        ),
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'media.sqlite3'}")
    site.provide_capability(
        DatabaseCapability,
        SqlAlchemyDatabaseCapability.from_connections({"default": database}),
    )
    _CREATED_SITES.append(site)
    return site


@pytest.fixture(autouse=True)
def close_created_sites():
    yield
    while _CREATED_SITES:
        asyncio.run(_CREATED_SITES.pop().close())


def _capability(
    tmp_path: Path,
    *,
    url_mode: str = "storage-key",
) -> FilesystemMediaCapability:
    site = _site_with_media_database(tmp_path)
    return FilesystemMediaCapability(
        MediaSettings(root=tmp_path, url_mode=url_mode),
        database=site.capability_proxy(DatabaseCapability),
    )


def test_media_settings_resolve_defaults_from_project_root(tmp_path: Path) -> None:
    settings = MediaSettings.load_settings(_config(tmp_path))

    assert settings.root == tmp_path / "media"
    assert settings.mount_path == "/media"
    assert settings.serve is True
    assert settings.url_mode == "storage-key"


def test_media_settings_resolve_configured_values(tmp_path: Path) -> None:
    settings = MediaSettings.load_settings(
        _config(
            tmp_path,
            {
                "root": "uploads",
                "mount_path": "uploads",
                "serve": False,
                "url_mode": "id",
            },
        )
    )

    assert settings.root == tmp_path / "uploads"
    assert settings.mount_path == "/uploads"
    assert settings.serve is False
    assert settings.url_mode == "id"


def test_media_metadata_exposes_media_item_table() -> None:
    table = metadata.tables["media_item"]

    assert table.c.category.nullable is False
    assert table.c.storage_key.nullable is False


def test_media_metadata_exposes_media_resource_key_table() -> None:
    table = metadata.tables["media_resource_key"]

    assert table.c.media_id.nullable is False
    assert table.c.resource_key.nullable is False


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
) -> None:
    capability = _capability(tmp_path)

    with pytest.raises(InputValidationError, match=re.escape(message)) as excinfo:
        await capability.register(category=category, storage_key="profiles/avatar.png")

    assert isinstance(excinfo.value, MediaInputError)


@pytest.mark.anyio
async def test_media_capability_rejects_negative_media_size(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

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
) -> None:
    capability = _capability(tmp_path)

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
    site = _site_with_media_database(tmp_path)
    capability = FilesystemMediaCapability(
        MediaSettings(root=tmp_path / "missing"),
        database=site.capability_proxy(DatabaseCapability),
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
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

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
async def test_media_capability_registers_and_resolves_resource_key(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

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
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

    item = await capability.store(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        upload=FakeUpload((b"ava", b"tar"), "image/png"),
        resource_key="profile-picture",
    )

    resolved = await capability.get_by_resource_key("profile-picture")

    assert resolved.id == item.id


@pytest.mark.anyio
async def test_media_capability_reassigns_resource_key(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

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
        second.id,
        "default-avatar",
    )

    assert (await capability.get_by_resource_key("default-avatar")).id == second.id


@pytest.mark.anyio
async def test_media_capability_stores_upload_and_registers_catalogue_item(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

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
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)
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
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)
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
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path, url_mode="id")
    await create_database_schema(capability)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        size=123,
    )

    assert await capability.url_for(item.id) == f"/media/items/{item.id}"


@pytest.mark.anyio
async def test_media_capability_rejects_unknown_resource_key(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    capability = _capability(tmp_path)
    await create_database_schema(capability)

    with pytest.raises(MediaNotFoundError) as excinfo:
        await capability.get_by_resource_key("missing")
    assert isinstance(excinfo.value, MediaNotFoundError)


@pytest.mark.anyio
async def test_media_item_route_returns_not_found_for_missing_media(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    app = FastAPI()
    capability = _capability(tmp_path, url_mode="id")
    await create_database_schema(capability)
    site = Site(app=app, config=_config(tmp_path))

    media_module._register_media_item_route(site, capability)

    response = TestClient(app).get(f"/media/items/{uuid.uuid4()}")

    assert response.status_code == 404


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

    response = TestClient(app).get(f"/media/items/{media_id}")

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

    response = TestClient(app, raise_server_exceptions=False).get(
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
                    "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
                },
                "wybra.media": {"root": "media", "mount_path": "/media"},
            }
        ),
    )

    assert (
        site.require_capability(MediaCapability).path_for_key("avatar.txt")
        == (media_root / "avatar.txt").resolve()
    )
    assert TestClient(app).get("/media/avatar.txt").text == "avatar"


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
                    "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
                },
                "wybra.media": {"root": "media", "mount_path": "/media"},
            }
        ),
    )

    assert site.has_capability(MediaCapability) is True
    assert site.has_capability(DatabaseCapability) is True


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

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "project_root": tmp_path,
                    "modules": ("wybra.media", "wybra.db"),
                    "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
                },
                "wybra.media": {"serve": False},
            }
        ),
    )

    assert TestClient(app).get("/media/avatar.txt").status_code == 404
