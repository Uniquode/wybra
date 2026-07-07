from __future__ import annotations

import uuid
from typing import Protocol

from tortoise.exceptions import IntegrityError

from wybra.db import DatabaseCapability
from wybra.media.models import MediaItem, MediaResourceKey
from wybra.site import SiteCapabilityProxy


class MediaResourceKeyConflictError(RuntimeError):
    """Raised when a resource key belongs to another media item."""


class MediaCatalogueRepository(Protocol):
    """Catalogue persistence operations required by media capability."""

    async def create_item(
        self,
        *,
        category: str,
        storage_key: str,
        content_type: str | None,
        size: int,
        resource_key: str | None = None,
    ) -> MediaItem: ...

    async def get_item(self, media_id: uuid.UUID) -> MediaItem | None: ...

    async def get_item_by_resource_key(
        self,
        resource_key: str,
    ) -> MediaItem | None: ...

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None: ...


class TortoiseMediaCatalogueRepository:
    """Tortoise-backed media catalogue repository."""

    def __init__(self, database: SiteCapabilityProxy[DatabaseCapability]) -> None:
        self.database = database

    async def create_item(
        self,
        *,
        category: str,
        storage_key: str,
        content_type: str | None,
        size: int,
        resource_key: str | None = None,
    ) -> MediaItem:
        try:
            async with self.database.transaction() as connection:
                if resource_key is not None:
                    conflicting = await MediaResourceKey.get_or_none(
                        resource_key=resource_key,
                        using_db=connection,
                    )
                    if conflicting is not None:
                        raise MediaResourceKeyConflictError(resource_key)
                item = await MediaItem.create(
                    category=category,
                    storage_key=storage_key,
                    content_type=content_type,
                    size=size,
                    using_db=connection,
                )
                if resource_key is not None:
                    await MediaResourceKey.create(
                        media_id=item.id,
                        resource_key=resource_key,
                        using_db=connection,
                    )
        except IntegrityError as exc:
            if resource_key is not None and _resource_key_conflict(exc):
                raise MediaResourceKeyConflictError(resource_key) from exc
            raise
        return item

    async def get_item(self, media_id: uuid.UUID) -> MediaItem | None:
        async with self.database.transaction() as connection:
            return await MediaItem.get_or_none(id=media_id, using_db=connection)

    async def get_item_by_resource_key(
        self,
        resource_key: str,
    ) -> MediaItem | None:
        async with self.database.transaction() as connection:
            resource = await MediaResourceKey.get_or_none(
                resource_key=resource_key,
                using_db=connection,
            )
            if resource is None:
                return None
            return await MediaItem.get_or_none(
                id=resource.media_id,
                using_db=connection,
            )

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None:
        async with self.database.transaction() as connection:
            existing = await MediaResourceKey.get_or_none(
                resource_key=resource_key,
                using_db=connection,
            )
            if existing is None:
                await MediaResourceKey.create(
                    media_id=media_id,
                    resource_key=resource_key,
                    using_db=connection,
                )
            elif existing.media_id == media_id:
                return
            else:
                raise MediaResourceKeyConflictError(resource_key)


def _resource_key_conflict(exc: IntegrityError) -> bool:
    message = str(exc).lower()
    return "media_resource_key" in message or "resource_key" in message
