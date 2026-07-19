"""Test-only models with relations into a committed migration app."""

from wybra.db import fields
from wybra.db.models import Model


class SessionReference(Model):
    id = fields.IntField(primary_key=True)
    session = fields.ForeignKeyField("wybra_sessions.SessionRecordModel")

    class Meta:
        table = "test_session_reference"


__all__ = ("SessionReference",)
