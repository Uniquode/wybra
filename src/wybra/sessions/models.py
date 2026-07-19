from __future__ import annotations

from wybra.db import fields
from wybra.db.models import Model


class SessionRecordModel(Model):
    """Server-side request session persisted by the database backend."""

    id = fields.CharField(max_length=128, primary_key=True)
    data = fields.TextField()
    created_at = fields.FloatField()
    updated_at = fields.FloatField()
    expires_at = fields.FloatField(db_index=True)

    class Meta:
        table = "sessions_session"


__all__ = ("SessionRecordModel",)
