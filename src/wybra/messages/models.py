from __future__ import annotations

import time

from tortoise import fields
from tortoise.indexes import Index
from tortoise.models import Model


class MessageAlert(Model):
    """Queued user-facing alert stored by the database messages backend."""

    id = fields.IntField(primary_key=True)
    queue_key = fields.CharField(max_length=255)
    severity = fields.CharField(max_length=16)
    message = fields.TextField()
    created_at = fields.FloatField(default=time.time)
    expires_at = fields.FloatField(null=True, db_index=True)

    class Meta:
        table = "messages_alert"
        indexes = (Index(fields=("queue_key", "id")),)


__all__ = ("MessageAlert",)
