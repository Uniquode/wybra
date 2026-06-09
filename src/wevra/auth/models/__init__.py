from __future__ import annotations

import uuid

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from wevra.auth.email_normalisation import normalise_email
from wevra.auth.timestamps import current_timestamp
from wevra.db.models import Base


class InitialAdminBootstrap(Base):
    """Serialises initial admin bootstrap state."""

    __tablename__ = "identity_initial_admin_bootstrap"

    id: Mapped[int] = mapped_column(primary_key=True)


class IdentityProvider(Base):
    """Canonical provider identity row used by external login flows."""

    __tablename__ = "identity_provider"
    __table_args__ = (
        UniqueConstraint(
            "provider_name",
            "provider_subject",
            name="uq_identity_provider_name_subject",
        ),
        Index("ix_identity_provider_name", "provider_name"),
        Index("ix_identity_provider_subject", "provider_subject"),
        Index("ix_identity_provider_enabled", "provider_enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    provider_name: Mapped[str] = mapped_column(String(length=100), nullable=False)
    provider_subject: Mapped[str] = mapped_column(
        String(length=320),
        nullable=False,
    )
    crypt_access_token: Mapped[str] = mapped_column(
        String(length=1024),
        nullable=False,
    )
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    crypt_refresh_token: Mapped[str | None] = mapped_column(
        String(length=1024),
        nullable=True,
    )
    account_email: Mapped[str] = mapped_column(String(length=320), nullable=False)
    provider_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    provider_metadata: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    links: Mapped[list[ExternalIdentityLink]] = relationship(
        "ExternalIdentityLink",
        back_populates="provider",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ExternalIdentityLink(Base):
    """Link row between a local user and one provider identity."""

    __tablename__ = "identity_external_identity_link"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            name="uq_identity_external_identity_link_provider_id",
        ),
        Index(
            "ix_identity_external_identity_link_user_id",
            "user_id",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_provider.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="external_identity_links",
    )
    provider: Mapped[IdentityProvider] = relationship(
        "IdentityProvider",
        back_populates="links",
    )


class IdentityUserEmail(Base):
    """Additional email addresses for local user accounts."""

    __tablename__ = "identity_user_email"
    __table_args__ = (
        UniqueConstraint("email", name="uq_identity_user_email_email"),
        Index("ix_identity_user_email_user_id", "user_id"),
        Index(
            "uq_identity_user_email_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
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
    email: Mapped[str] = mapped_column(String(length=320), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship("User", back_populates="emails")

    @validates("email")
    def _normalise_identity_email(self, key: str, value: str) -> str:
        del key
        return normalise_email(value)


class User(SQLAlchemyBaseUserTableUUID, Base):
    """Canonical local user account."""

    __tablename__ = "identity_user"
    __table_args__ = (
        Index("ix_identity_user_is_active_expires_at", "is_active", "expires_at"),
        Index("ix_identity_user_last_login_at", "last_login_at"),
        Index("ix_identity_user_created_at", "created_at"),
        Index("ix_identity_user_modified_at", "modified_at"),
        Index("ix_identity_user_is_admin", "is_admin"),
        Index("ix_identity_user_is_superuser", "is_superuser"),
    )

    # Store Unix timestamps as `float` for cross-database consistency.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[float] = mapped_column(
        Float,
        default=current_timestamp,
        nullable=False,
    )
    modified_at: Mapped[float] = mapped_column(
        Float,
        default=current_timestamp,
        onupdate=current_timestamp,
        nullable=False,
    )
    last_login_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    email_verification_sent_at: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(length=320), nullable=True)
    preferred_name: Mapped[str | None] = mapped_column(
        String(length=120),
        nullable=True,
    )
    preferred_timezone: Mapped[str | None] = mapped_column(
        String(length=64),
        nullable=True,
    )
    emails: Mapped[list[IdentityUserEmail]] = relationship(
        "IdentityUserEmail",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    external_identity_links: Mapped[list[ExternalIdentityLink]] = relationship(
        "ExternalIdentityLink",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Group(Base):
    """Authorisation group used to collect reusable scopes."""

    __tablename__ = "identity_group"
    __table_args__ = (Index("ix_identity_group_abbrev", "abbrev", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    abbrev: Mapped[str] = mapped_column(String(length=120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Scope(Base):
    """Authorisation scope assignable to groups."""

    __tablename__ = "identity_scope"

    scope: Mapped[str] = mapped_column(
        String(length=255), nullable=False, primary_key=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class GroupScope(Base):
    """Scope assignment on an authorisation group."""

    __tablename__ = "identity_group_scope"
    __table_args__ = (
        UniqueConstraint("group_id", "scope", name="uq_identity_group_scope_pair"),
        Index("ix_identity_group_scope_group_id", "group_id"),
        Index("ix_identity_group_scope_scope", "scope"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_group.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    scope: Mapped[str] = mapped_column(
        String(length=255),
        ForeignKey("identity_scope.scope", ondelete="RESTRICT"),
        primary_key=True,
    )


class GroupUser(Base):
    """Direct user membership in an authorisation group."""

    __tablename__ = "identity_group_user"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_identity_group_user_pair"),
        Index("ix_identity_group_user_group_id", "group_id"),
        Index("ix_identity_group_user_user_id", "user_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_group.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="CASCADE"),
        primary_key=True,
    )


class GroupGroup(Base):
    """Nested group membership in an authorisation group tree."""

    __tablename__ = "identity_group_group"
    __table_args__ = (
        UniqueConstraint(
            "parent_group_id",
            "child_group_id",
            name="uq_identity_group_group_pair",
        ),
        Index("ix_identity_group_group_parent_group_id", "parent_group_id"),
        Index("ix_identity_group_group_child_group_id", "child_group_id"),
        CheckConstraint(
            "parent_group_id <> child_group_id",
            name="ck_identity_group_group_no_self_membership",
        ),
    )

    parent_group_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_group.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    child_group_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_group.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """Server-side browser session token managed by FastAPI Users."""

    __tablename__ = "identity_access_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="cascade"),
        nullable=False,
    )


metadata = Base.metadata

__all__ = (
    "AccessToken",
    "Base",
    "ExternalIdentityLink",
    "IdentityUserEmail",
    "IdentityProvider",
    "Group",
    "GroupGroup",
    "GroupScope",
    "GroupUser",
    "InitialAdminBootstrap",
    "Scope",
    "User",
    "metadata",
)
