from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import anyio
from sqlalchemy import select

from wevra.db import DatabaseCapability
from wevra.media.config import MediaSettings
from wevra.media.models import MediaItem, MediaResourceKey
from wevra.site import SiteCapabilityProxy


class MediaCapabilityError(RuntimeError):
    """Raised when a media capability operation cannot be completed."""


@runtime_checkable
class MediaCapability(Protocol):
    """Public media capability exposed through ``Site``."""

    @property
    def root(self) -> Path: ...

    @property
    def mount_path(self) -> str: ...

    @property
    def serve(self) -> bool: ...

    @property
    def url_mode(self) -> str: ...

    async def register(
        self,
        *,
        category: str,
        storage_key: str | Path,
        content_type: str | None = None,
        size: int = 0,
        resource_key: str | None = None,
    ) -> MediaItem: ...

    async def store(
        self,
        *,
        category: str,
        storage_key: str | Path,
        upload: MediaUpload,
        chunk_size: int = 1024 * 1024,
        resource_key: str | None = None,
    ) -> MediaItem: ...

    async def get(self, media_id: uuid.UUID) -> MediaItem: ...

    async def path_for(self, media_id: uuid.UUID) -> Path: ...

    async def url_for(self, media_id: uuid.UUID) -> str: ...

    async def get_by_resource_key(self, resource_key: str) -> MediaItem: ...

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None: ...

    def path_for_key(self, storage_key: str | Path) -> Path: ...

    def url_for_key(self, storage_key: str | Path) -> str: ...

    def validate_writable(self) -> None: ...


class MediaUpload(Protocol):
    """Async upload stream accepted by media storage workflows."""

    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class FilesystemMediaCapability:
    settings: MediaSettings
    database: SiteCapabilityProxy[DatabaseCapability]
    _writable_root_validated: bool = field(default=False, init=False, repr=False)

    @property
    def root(self) -> Path:
        return self.settings.root

    @property
    def mount_path(self) -> str:
        return self.settings.mount_path

    @property
    def serve(self) -> bool:
        return self.settings.serve

    @property
    def url_mode(self) -> str:
        return self.settings.url_mode

    async def register(
        self,
        *,
        category: str,
        storage_key: str | Path,
        content_type: str | None = None,
        size: int = 0,
        resource_key: str | None = None,
    ) -> MediaItem:
        media_resource_key = _resource_key_value(resource_key)
        item = MediaItem(
            category=_category_value(category),
            storage_key=_storage_key(storage_key),
            content_type=content_type,
            size=_size_value(size),
        )
        async with self.database.transaction() as session:
            session.add(item)
            await session.flush()
            if media_resource_key is not None:
                session.add(
                    MediaResourceKey(
                        media_id=item.id,
                        resource_key=media_resource_key,
                    )
                )
            await session.refresh(item)
        return item

    async def store(
        self,
        *,
        category: str,
        storage_key: str | Path,
        upload: MediaUpload,
        chunk_size: int = 1024 * 1024,
        resource_key: str | None = None,
    ) -> MediaItem:
        if chunk_size <= 0:
            raise MediaCapabilityError("Media upload chunk size must be positive.")
        media_category = _category_value(category)
        media_key = _storage_key(storage_key)
        self.validate_writable()
        destination = self.path_for_key(media_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_root = self.root.resolve() / ".tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_destination = temp_root / f"{uuid.uuid4().hex}.{destination.name}.tmp"

        size = 0
        try:
            async with await anyio.open_file(temp_destination, "wb") as output:
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    await output.write(chunk)

            temp_destination.replace(destination)
            try:
                return await self.register(
                    category=media_category,
                    storage_key=media_key,
                    content_type=upload.content_type,
                    size=size,
                    resource_key=resource_key,
                )
            except BaseException:
                with suppress(FileNotFoundError):
                    destination.unlink()
                raise
        except BaseException:
            with suppress(FileNotFoundError):
                temp_destination.unlink()
            raise

    async def get(self, media_id: uuid.UUID) -> MediaItem:
        async with self.database.session() as session:
            item = await session.scalar(
                select(MediaItem).where(MediaItem.id == media_id)
            )
        if item is None:
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")
        return item

    async def path_for(self, media_id: uuid.UUID) -> Path:
        return self.path_for_key((await self.get(media_id)).storage_key)

    async def url_for(self, media_id: uuid.UUID) -> str:
        item = await self.get(media_id)
        if self.url_mode == "id":
            return f"{self.mount_path}/items/{item.id}"
        return self.url_for_key(item.storage_key)

    async def get_by_resource_key(self, resource_key: str) -> MediaItem:
        validated_resource_key = _resource_key_value(resource_key)
        async with self.database.session() as session:
            media = await session.scalar(
                select(MediaItem)
                .join(
                    MediaResourceKey,
                    MediaItem.id == MediaResourceKey.media_id,
                )
                .where(MediaResourceKey.resource_key == validated_resource_key)
            )
        if media is None:
            raise MediaCapabilityError(
                f"Unknown media resource key: resource_key={resource_key}."
            )
        return media

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None:
        validated_resource_key = _resource_key_value(resource_key)
        _ = await self.get(media_id)
        async with self.database.transaction() as session:
            existing = await session.scalar(
                select(MediaResourceKey).where(
                    MediaResourceKey.resource_key == validated_resource_key
                )
            )
            if existing is None:
                session.add(
                    MediaResourceKey(
                        media_id=media_id,
                        resource_key=validated_resource_key,
                    )
                )
            else:
                existing.media_id = media_id
                session.add(existing)

    def path_for_key(self, storage_key: str | Path) -> Path:
        key_path = _key_path(storage_key)
        root = self.root.resolve()
        resolved = (root / key_path).resolve()
        if not resolved.is_relative_to(root):
            raise MediaCapabilityError(
                f"Media key escapes media root: key={storage_key!s}."
            )
        return resolved

    def url_for_key(self, storage_key: str | Path) -> str:
        resolved = self.path_for_key(storage_key)
        key_path = resolved.relative_to(self.root.resolve())
        return f"{self.mount_path}/{key_path.as_posix()}"

    def validate_writable(self) -> None:
        root = self.root.resolve()
        if self._writable_root_validated:
            return
        if not root.exists():
            raise MediaCapabilityError(f"Media root does not exist: root={self.root}.")
        if not root.is_dir():
            raise MediaCapabilityError(
                f"Media root is not a directory: root={self.root}."
            )
        probe = root / f".wevra-media-write-test-{uuid.uuid4().hex}"
        try:
            probe.touch(exist_ok=True)
        except OSError as exc:
            raise MediaCapabilityError(
                f"Media root is not writable: root={self.root}."
            ) from exc
        finally:
            probe.unlink(missing_ok=True)
        object.__setattr__(self, "_writable_root_validated", True)


def _key_path(key: str | Path) -> Path:
    key_path = Path(key)
    if key_path.is_absolute():
        raise MediaCapabilityError(f"Media key must be relative: key={key!s}.")
    if not key_path.parts:
        raise MediaCapabilityError("Media key must not be blank.")
    if any(part in {"", ".", ".."} for part in key_path.parts):
        raise MediaCapabilityError(f"Media key is invalid: key={key!s}.")
    return key_path


def _storage_key(value: str | Path) -> str:
    return _key_path(value).as_posix()


def _category_value(value: str) -> str:
    if not value.strip():
        raise MediaCapabilityError("Media category must not be blank.")
    if "/" in value or "\\" in value:
        raise MediaCapabilityError("Media category must not contain path separators.")
    return value.strip()


def _size_value(value: int) -> int:
    if value < 0:
        raise MediaCapabilityError("Media size must not be negative.")
    return value


def _resource_key_value(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MediaCapabilityError("Media resource key must be text.")
    resource_key = value.strip()
    if not resource_key:
        raise MediaCapabilityError("Media resource key must not be blank.")
    return resource_key


__all__ = (
    "FilesystemMediaCapability",
    "MediaCapability",
    "MediaCapabilityError",
    "MediaUpload",
)
