from __future__ import annotations

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wybra.db.models import Base, metadata


class SessionRecordModel(Base):
    """Server-side request session persisted by the database backend."""

    __tablename__ = "sessions_session"

    id: Mapped[str] = mapped_column(String(length=128), primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)


__all__ = ("SessionRecordModel", "metadata")
