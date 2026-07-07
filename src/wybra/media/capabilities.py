from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import anyio

from wybra.core import InputValidationError
from wybra.media.config import MediaSettings
from wybra.media.models import MediaItem
from wybra.media.persistence import (
    MediaCatalogueRepository,
    MediaResourceKeyConflictError,
)


class MediaError(RuntimeError):
    """Base for media-domain failures."""


class MediaCapabilityError(MediaError):
    """Raised when a media capability cannot be resolved or used."""


class MediaInputError(InputValidationError, MediaError):
    """Raised when caller-provided media input is invalid."""


class MediaNotFoundError(MediaError):
    """Raised when a requested media item or resource key does not exist."""


class MediaStorageReadinessError(MediaError):
    """Raised when media storage is not configured or ready for use."""


class MediaStorageOperationError(MediaError):
    """Raised when media storage IO fails during an operation."""


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
    catalogue: MediaCatalogueRepository
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
        return await self.catalogue.create_item(
            category=_category_value(category),
            storage_key=_storage_key(storage_key),
            content_type=content_type,
            size=_size_value(size),
            resource_key=media_resource_key,
        )

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
            raise MediaInputError("Media upload chunk size must be positive.")
        media_category = _category_value(category)
        media_key = _storage_key(storage_key)
        self.validate_writable()
        destination = self.path_for_key(media_key)
        temp_root = self.root.resolve() / ".tmp"
        temp_destination = temp_root / f"{uuid.uuid4().hex}.{destination.name}.tmp"

        size = 0
        # Filesystem writes and the atomic replace are storage operations; keep
        # non-IO failures unchanged while still removing any temporary upload.
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_root.mkdir(parents=True, exist_ok=True)
            async with await anyio.open_file(temp_destination, "wb") as output:
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    await output.write(chunk)

            temp_destination.replace(destination)
        except OSError as exc:
            with suppress(FileNotFoundError):
                temp_destination.unlink()
            raise MediaStorageOperationError(
                f"Media storage operation failed: key={media_key}."
            ) from exc
        except BaseException:
            with suppress(FileNotFoundError):
                temp_destination.unlink()
            raise

        # Registration completes the store operation. If catalogue registration
        # fails, remove the written file so callers do not inherit an orphan.
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

    async def get(self, media_id: uuid.UUID) -> MediaItem:
        item = await self.catalogue.get_item(media_id)
        if item is None:
            raise MediaNotFoundError(f"Unknown media item: media_id={media_id}.")
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
        if validated_resource_key is None:
            raise MediaInputError("Media resource key must not be blank.")
        media = await self.catalogue.get_item_by_resource_key(validated_resource_key)
        if media is None:
            raise MediaNotFoundError(
                f"Unknown media resource key: resource_key={resource_key}."
            )
        return media

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None:
        validated_resource_key = _resource_key_value(resource_key)
        if validated_resource_key is None:
            raise MediaInputError("Media resource key must not be blank.")
        _ = await self.get(media_id)
        try:
            await self.catalogue.assign_resource_key(media_id, validated_resource_key)
        except MediaResourceKeyConflictError as exc:
            raise MediaInputError(
                f"Media resource key is already assigned: "
                f"resource_key={validated_resource_key}."
            ) from exc

    def path_for_key(self, storage_key: str | Path) -> Path:
        key_path = _key_path(storage_key)
        root = self.root.resolve()
        resolved = (root / key_path).resolve()
        if not resolved.is_relative_to(root):
            raise MediaInputError(f"Media key escapes media root: key={storage_key!s}.")
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
            raise MediaStorageReadinessError(
                f"Media root does not exist: root={self.root}."
            )
        if not root.is_dir():
            raise MediaStorageReadinessError(
                f"Media root is not a directory: root={self.root}."
            )
        probe = root / f".wybra-media-write-test-{uuid.uuid4().hex}"
        try:
            probe.touch(exist_ok=True)
        except OSError as exc:
            raise MediaStorageOperationError(
                f"Media root is not writable: root={self.root}."
            ) from exc
        finally:
            probe.unlink(missing_ok=True)
        object.__setattr__(self, "_writable_root_validated", True)


def _key_path(key: str | Path) -> Path:
    key_path = Path(key)
    if key_path.is_absolute():
        raise MediaInputError(f"Media key must be relative: key={key!s}.")
    if not key_path.parts:
        raise MediaInputError("Media key must not be blank.")
    if any(part in {"", ".", ".."} for part in key_path.parts):
        raise MediaInputError(f"Media key is invalid: key={key!s}.")
    return key_path


def _storage_key(value: str | Path) -> str:
    return _key_path(value).as_posix()


def _category_value(value: str) -> str:
    if not value.strip():
        raise MediaInputError("Media category must not be blank.")
    if "/" in value or "\\" in value:
        raise MediaInputError("Media category must not contain path separators.")
    return value.strip()


def _size_value(value: int) -> int:
    if value < 0:
        raise MediaInputError("Media size must not be negative.")
    return value


def _resource_key_value(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MediaInputError("Media resource key must be text.")
    resource_key = value.strip()
    if not resource_key:
        raise MediaInputError("Media resource key must not be blank.")
    return resource_key


__all__ = (
    "FilesystemMediaCapability",
    "MediaCapability",
    "MediaCapabilityError",
    "MediaError",
    "MediaInputError",
    "MediaNotFoundError",
    "MediaStorageOperationError",
    "MediaStorageReadinessError",
    "MediaUpload",
)
