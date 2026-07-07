from __future__ import annotations

import time
import uuid

from tortoise import fields
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


class MediaResourceKey(Model):
    """Lookup key assigned to a media item for stable resource references."""

    resource_key = fields.CharField(max_length=255, primary_key=True)
    media_id = fields.UUIDField(db_index=True)

    class Meta:
        table = "media_resource_key"


__all__ = ("MediaItem", "MediaResourceKey")
