from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.indexes import Index
from tortoise.models import Model


class MediaItem(Model):
    """Catalogued media item stored under the configured media root."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    category = fields.CharField(max_length=120)
    storage_key = fields.CharField(max_length=1024, unique=True)
    content_type = fields.CharField(max_length=255, null=True)
    size = fields.IntField()
    created_at = fields.FloatField(default=time.time)
    modified_at = fields.FloatField(default=time.time)

    class Meta:
        table = "media_item"
        indexes = (
            Index(fields=("category",)),
            Index(fields=("created_at",)),
        )

    async def save(
        self,
        using_db: BaseDBAsyncClient | None = None,
        update_fields: Iterable[str] | None = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        self.modified_at = time.time()
        if update_fields is not None and "modified_at" not in update_fields:
            update_fields = (*update_fields, "modified_at")
        await super().save(
            using_db=using_db,
            update_fields=update_fields,
            force_create=force_create,
            force_update=force_update,
        )


class MediaResourceKey(Model):
    """Lookup key assigned to a media item for stable resource references."""

    if TYPE_CHECKING:
        media_id: uuid.UUID

    resource_key = fields.CharField(max_length=255, primary_key=True)
    media = fields.ForeignKeyField(
        "wybra_media.MediaItem",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )

    class Meta:
        table = "media_resource_key"


__all__ = ("MediaItem", "MediaResourceKey")
