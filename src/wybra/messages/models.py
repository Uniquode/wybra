from __future__ import annotations

import time

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wybra.db.models import Base, metadata


class MessageAlert(Base):
    """Queued user-facing alert stored by the database messages backend."""

    __tablename__ = "messages_alert"
    __table_args__ = (
        Index("ix_messages_alert_queue_key_id", "queue_key", "id"),
        Index("ix_messages_alert_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    queue_key: Mapped[str] = mapped_column(String(length=255), nullable=False)
    severity: Mapped[str] = mapped_column(String(length=16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(
        Float,
        default=time.time,
        nullable=False,
    )
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)


__all__ = ("MessageAlert", "metadata")
