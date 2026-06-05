from __future__ import annotations

import uuid

from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseOAuthAccountTableUUID,
    SQLAlchemyBaseUserTableUUID,
)
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from auth_ext.timestamps import current_timestamp
from data_core.models import Base


class InitialAdminBootstrap(Base):
    """Singleton claim row that serialises initial admin bootstrap."""

    __tablename__ = "identity_initial_admin_bootstrap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class OAuthAccount(SQLAlchemyBaseOAuthAccountTableUUID, Base):
    """Linked external OAuth account for a canonical local user."""

    __tablename__ = "identity_oauth_account"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="cascade"),
        nullable=False,
    )


class User(SQLAlchemyBaseUserTableUUID, Base):
    """Canonical local account used by browser, API, and linked identities."""

    __tablename__ = "identity_user"
    __table_args__ = (
        Index("ix_identity_user_is_active_expires_at", "is_active", "expires_at"),
        Index("ix_identity_user_last_login_at", "last_login_at"),
        Index("ix_identity_user_created_at", "created_at"),
        Index("ix_identity_user_modified_at", "modified_at"),
        Index("ix_identity_user_is_admin", "is_admin"),
        Index("ix_identity_user_is_superuser", "is_superuser"),
    )

    # Store Unix seconds from the application clock by design. This keeps the
    # reusable auth extension portable across supported SQL backends and aligns
    # with the user-management CLI contract.
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

    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        "OAuthAccount",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Group(Base):
    """Authorisation group used to collect reusable scopes."""

    __tablename__ = "identity_group"
    __table_args__ = (Index("ix_identity_group_abbrev", "abbrev", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    abbrev: Mapped[str] = mapped_column(String(length=120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Scope(Base):
    """Described authorisation scope assignable to groups."""

    __tablename__ = "identity_scope"

    scope: Mapped[str] = mapped_column(String(length=255), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class GroupScope(Base):
    """Scope assigned directly to an authorisation group."""

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
    """Nested child-group membership in an authorisation group."""

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
    "Group",
    "GroupGroup",
    "GroupScope",
    "GroupUser",
    "InitialAdminBootstrap",
    "OAuthAccount",
    "Scope",
    "User",
    "metadata",
)
