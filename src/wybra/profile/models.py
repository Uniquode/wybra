from __future__ import annotations

import uuid

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from wybra.db.models import Base, metadata
from wybra.profile.types import ProfileLinks, Pronouns


class UserProfile(Base):
    """App-facing profile data linked one-to-one with an auth user."""

    __tablename__ = "profile_user_profile"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_profile_user_profile_user_id"),
        Index("ix_profile_user_profile_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_picture_media_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("media_item.id", ondelete="SET NULL"),
        nullable=True,
    )
    preferred_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pronouns: Mapped[Pronouns | None] = mapped_column(JSON, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(48), nullable=True)
    website_links: Mapped[ProfileLinks | None] = mapped_column(JSON, nullable=True)
    country_region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_industry: Mapped[str | None] = mapped_column(String(160), nullable=True)
    department: Mapped[str | None] = mapped_column(String(160), nullable=True)
    date_time_format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    theme: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notification_preferences: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    profile_visibility: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="public",
    )
    marketing_consent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    terms_accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_deletion_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class UserPhoneContact(Base):
    """Per-user phone contact with per-number verification state."""

    __tablename__ = "profile_phone_contact"
    __table_args__ = (
        Index("ix_profile_phone_contact_user_id", "user_id"),
        Index("ix_profile_phone_contact_normalised_number", "normalised_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    subdivision_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    normalised_number: Mapped[str] = mapped_column(String(32), nullable=False)
    number_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sms_capable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Unix timestamp seconds when this phone contact was verified.
    verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)


__all__ = ("UserPhoneContact", "UserProfile", "metadata")
