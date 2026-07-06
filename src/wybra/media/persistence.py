from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select

from wybra.db import DatabaseCapability
from wybra.media.models import MediaItem, MediaResourceKey
from wybra.site import SiteCapabilityProxy


class MediaCatalogueRepository(Protocol):
    """Storage-neutral catalogue operations required by media capability."""

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


class SqlAlchemyMediaCatalogueRepository:
    """SQLAlchemy-backed media catalogue adapter."""

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
        item = MediaItem(
            category=category,
            storage_key=storage_key,
            content_type=content_type,
            size=size,
        )
        async with self.database.transaction() as session:
            session.add(item)
            await session.flush()
            if resource_key is not None:
                session.add(
                    MediaResourceKey(
                        media_id=item.id,
                        resource_key=resource_key,
                    )
                )
            await session.refresh(item)
        return item

    async def get_item(self, media_id: uuid.UUID) -> MediaItem | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(MediaItem).where(MediaItem.id == media_id)
            )

    async def get_item_by_resource_key(
        self,
        resource_key: str,
    ) -> MediaItem | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(MediaItem)
                .join(
                    MediaResourceKey,
                    MediaItem.id == MediaResourceKey.media_id,
                )
                .where(MediaResourceKey.resource_key == resource_key)
            )

    async def assign_resource_key(
        self,
        media_id: uuid.UUID,
        resource_key: str,
    ) -> None:
        async with self.database.transaction() as session:
            existing = await session.scalar(
                select(MediaResourceKey).where(
                    MediaResourceKey.resource_key == resource_key
                )
            )
            if existing is None:
                session.add(
                    MediaResourceKey(
                        media_id=media_id,
                        resource_key=resource_key,
                    )
                )
            else:
                existing.media_id = media_id
                session.add(existing)
