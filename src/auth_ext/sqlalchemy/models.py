from __future__ import annotations

import uuid

from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseOAuthAccountTableUUID,
    SQLAlchemyBaseUserTableUUID,
)
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for authentication extension SQLAlchemy models."""


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

    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        "OAuthAccount",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """Server-side browser session token managed by FastAPI Users."""

    __tablename__ = "identity_access_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("identity_user.id", ondelete="cascade"),
        nullable=False,
    )
