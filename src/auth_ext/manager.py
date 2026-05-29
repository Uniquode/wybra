from __future__ import annotations

import uuid

from fastapi import Request
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users.exceptions import InvalidPasswordException
from fastapi_users.password import PasswordHelper
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from auth_ext.delivery import IdentityDelivery, NullIdentityDelivery
from auth_ext.options import IdentityOptions
from auth_ext.schemas import UserCreate
from auth_ext.sqlalchemy.models import User
from auth_ext.sqlalchemy.users import create_user_database


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """FastAPI Users manager for the canonical local account model."""

    def __init__(
        self,
        user_db: SQLAlchemyUserDatabase[User, uuid.UUID],
        options: IdentityOptions,
        delivery: IdentityDelivery | None = None,
        password_helper: PasswordHelper | None = None,
    ) -> None:
        super().__init__(user_db, password_helper)
        self.delivery = delivery or NullIdentityDelivery()
        self.reset_password_token_secret = options.reset_password_token_secret
        self.verification_token_secret = options.verification_token_secret

    async def validate_password(
        self,
        password: str,
        user: UserCreate | User,
    ) -> None:
        if not password.strip():
            raise InvalidPasswordException("Password must not be blank.")

    async def on_after_forgot_password(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        await self.delivery.send_reset_password_token(user, token, request)

    async def on_after_request_verify(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        await self.delivery.send_verification_token(user, token, request)


def create_password_helper() -> PasswordHelper:
    return PasswordHelper()


def create_user_manager(
    session: AsyncSession,
    options: IdentityOptions,
    delivery: IdentityDelivery | None = None,
) -> UserManager:
    return UserManager(
        create_user_database(session),
        options,
        delivery,
        create_password_helper(),
    )
