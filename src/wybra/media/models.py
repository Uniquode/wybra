from __future__ import annotations

import time
import uuid

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wybra.db.models import Base, metadata


class MediaItem(Base):
    """Catalogued media item stored under the configured media root."""

    __tablename__ = "media_item"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_media_item_storage_key"),
        Index("ix_media_item_category", "category"),
        Index("ix_media_item_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    category: Mapped[str] = mapped_column(String(length=120), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(length=1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[float] = mapped_column(
        Float,
        default=time.time,
        nullable=False,
    )
    modified_at: Mapped[float] = mapped_column(
        Float,
        default=time.time,
        onupdate=time.time,
        nullable=False,
    )


class MediaResourceKey(Base):
    """Lookup key assigned to a media item for stable resource references."""

    __tablename__ = "media_resource_key"
    __table_args__ = (Index("ix_media_resource_key_media_id", "media_id"),)

    resource_key: Mapped[str] = mapped_column(String(length=255), primary_key=True)
    media_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("media_item.id", ondelete="CASCADE"),
        nullable=False,
    )


__all__ = ("MediaItem", "MediaResourceKey", "metadata")
