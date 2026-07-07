from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, TIMESTAMP, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID


class GUID(TypeDecorator[uuid.UUID]):
    """Platform-independent UUID column type for SQLAlchemy adapters."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID())
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return str(value)
        if not isinstance(value, uuid.UUID):
            return str(uuid.UUID(value))
        return str(value)

    def process_result_value(self, value, dialect):
        del dialect
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class TIMESTAMPAware(TypeDecorator[datetime]):
    """UTC-aware timestamp type matching the previous access-token column."""

    impl = TIMESTAMP
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and dialect.name != "postgresql":
            return value.replace(tzinfo=UTC)
        return value


def now_utc() -> datetime:
    return datetime.now(UTC)
