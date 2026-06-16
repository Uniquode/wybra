from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wevra.auth import models as auth_models  # noqa: F401
from wevra.config import ConfigService, MappingConfigSource
from wevra.db import DatabaseCapability, SqlAlchemyDatabaseCapability
from wevra.db.models import metadata
from wevra.db.persistence import create_database
from wevra.media import (
    FilesystemMediaCapability,
    MediaCapability,
    MediaCapabilityError,
    MediaSettings,
)
from wevra.media.models import MediaItem
from wevra.media.validation import validate_media
from wevra.site import Site, start


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
                        "modules": ("wevra.media",),
                    },
                    "wevra.media": media_config or {},
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
    return site


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

    with pytest.raises(MediaCapabilityError):
        capability.path_for_key(key)


def test_media_capability_validates_writable_root(tmp_path: Path) -> None:
    capability = _capability(tmp_path)

    capability.validate_writable()


def test_media_capability_rejects_missing_writable_root(tmp_path: Path) -> None:
    site = _site_with_media_database(tmp_path)
    capability = FilesystemMediaCapability(
        MediaSettings(root=tmp_path / "missing"),
        database=site.capability_proxy(DatabaseCapability),
    )

    with pytest.raises(MediaCapabilityError, match="does not exist"):
        capability.validate_writable()


@pytest.mark.anyio
async def test_media_capability_registers_catalogue_item_and_resolves_by_id(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

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
) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        resource_key="country-codes",
    )

    resolved = await capability.get_by_resource_key("country-codes")

    assert resolved.id == item.id


@pytest.mark.anyio
async def test_media_capability_store_accepts_resource_key(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

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
) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

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
) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

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
async def test_media_capability_rejects_invalid_upload_chunk_size(
    tmp_path: Path,
) -> None:
    capability = _capability(tmp_path)

    with pytest.raises(MediaCapabilityError, match="chunk size"):
        await capability.store(
            category="profile",
            storage_key="profile/ab/cd/user.png",
            upload=FakeUpload((b"avatar",), "image/png"),
            chunk_size=0,
        )


@pytest.mark.anyio
async def test_media_capability_resolves_id_url_mode(tmp_path: Path) -> None:
    capability = _capability(tmp_path, url_mode="id")
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)

    item = await capability.register(
        category="profile",
        storage_key="profile/ab/cd/user.png",
        size=123,
    )

    assert await capability.url_for(item.id) == f"/media/items/{item.id}"


@pytest.mark.anyio
async def test_media_capability_rejects_unknown_resource_key(tmp_path: Path) -> None:
    capability = _capability(tmp_path)
    async with capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

    await session.run_sync(_create_all)

    with pytest.raises(MediaCapabilityError):
        await capability.get_by_resource_key("missing")


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
                    "modules": ("wevra.media",),
                },
                "wevra.media": {"root": "media", "mount_path": "/media"},
            }
        ),
    )

    assert (
        site.require_capability(MediaCapability).path_for_key("avatar.txt")
        == (media_root / "avatar.txt").resolve()
    )
    assert TestClient(app).get("/media/avatar.txt").text == "avatar"


@pytest.mark.anyio
async def test_media_setup_skips_serving_when_disabled(tmp_path: Path) -> None:
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "project_root": tmp_path,
                    "modules": ("wevra.media",),
                },
                "wevra.media": {"serve": False},
            }
        ),
    )

    assert TestClient(app).get("/media/avatar.txt").status_code == 404
