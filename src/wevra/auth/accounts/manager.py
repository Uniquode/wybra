from __future__ import annotations

import logging
import uuid
from typing import cast

from fastapi import Request
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users.exceptions import (
    InvalidPasswordException,
    UserAlreadyExists,
    UserNotExists,
)
from fastapi_users.password import PasswordHelper
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.accounts.schemas import UserCreate
from wevra.auth.delivery import IdentityDelivery, NullIdentityDelivery
from wevra.auth.emails import normalise_email_target, resolve_user_by_email
from wevra.auth.models import IdentityUserEmail, User
from wevra.auth.options import IdentityOptions
from wevra.auth.persistence import create_user_database
from wevra.auth.result import (
    ERROR_INVALID_PASSWORD,
    ERROR_PASSWORD_TOO_SHORT,
    ERROR_PASSWORD_TOO_WEAK,
    ResultErrorType,
)

logger = logging.getLogger(__name__)
_IDENTITY_USER_EMAIL_UNIQUE_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "uq_identity_user_email_email",
        "identity_user_email_email_key",
    }
)
PASSWORD_POLICY_MESSAGES: dict[ResultErrorType, str] = {
    ERROR_PASSWORD_TOO_SHORT: "Password does not meet the minimum length requirement.",
    ERROR_PASSWORD_TOO_WEAK: "Password does not meet the strength requirement.",
}
DEFAULT_PASSWORD_POLICY_MESSAGE = "Password is invalid."
PUBLIC_PASSWORD_POLICY_MESSAGES = frozenset(
    {
        DEFAULT_PASSWORD_POLICY_MESSAGE,
        *PASSWORD_POLICY_MESSAGES.values(),
    }
)


class PasswordPolicyException(InvalidPasswordException):
    """Invalid-password boundary carrying a branchable policy error type."""

    def __init__(self, error_type: ResultErrorType | None) -> None:
        self.error_type = error_type
        super().__init__(_password_policy_message(error_type))


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
        self.password_policy = options.resolved_password_policy()
        self.reset_password_token_secret = options.reset_password_token_secret
        self.verification_token_secret = options.verification_token_secret

    async def validate_password(
        self,
        password: str,
        user: UserCreate | User,
    ) -> None:
        validation = self.password_policy.validate(password, user)
        if validation.is_failure():
            logger.warning(
                "Password policy validation failed",
                extra={
                    "error_type": validation.error_type,
                    "policy_message": validation.message,
                    "user_id": getattr(user, "id", None),
                },
            )
            # Do not expose arbitrary policy messages here: custom policies may
            # include internal checks or vendor-specific detail in Result.message.
            raise PasswordPolicyException(validation.error_type)

    async def get_by_email(self, user_email: str) -> User:
        database = cast(SQLAlchemyUserDatabase[User, uuid.UUID], self.user_db)
        user = await resolve_user_by_email(database.session, user_email)
        if user is None:
            raise UserNotExists()
        return user

    async def create(
        self,
        user_create: UserCreate,
        safe: bool = False,
        request: Request | None = None,
    ) -> User:
        await self.validate_password(user_create.password, user_create)

        normalised_email = normalise_email_target(user_create.email)
        if normalised_email is None:
            raise UserAlreadyExists()

        try:
            await self.get_by_email(normalised_email)
            raise UserAlreadyExists()
        except UserNotExists:
            pass

        user_create_update = user_create.model_copy(update={"email": normalised_email})
        user_dict = (
            user_create_update.create_update_dict()
            if safe
            else user_create_update.create_update_dict_superuser()
        )
        password = user_dict.pop("password")
        user_dict["hashed_password"] = self.password_helper.hash(password)

        database = cast(SQLAlchemyUserDatabase[User, uuid.UUID], self.user_db)
        created_user = database.user_table(**user_dict)

        async def _create_and_persist_user() -> None:
            database.session.add(created_user)
            await database.session.flush()
            database.session.add(
                IdentityUserEmail(
                    user=created_user,
                    email=normalised_email,
                    is_primary=True,
                    is_verified=created_user.is_verified,
                )
            )
            await self.on_after_register(created_user, request)

        try:
            async with (
                database.session.begin()
                if not database.session.in_transaction()
                else database.session.begin_nested()
            ):
                await _create_and_persist_user()
        except IntegrityError as exc:
            if _is_identity_email_unique_violation(exc):
                raise UserAlreadyExists() from exc
            raise

        await database.session.refresh(created_user)
        return created_user

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


def _password_policy_message(error_type: ResultErrorType | None) -> str:
    if error_type is None:
        return DEFAULT_PASSWORD_POLICY_MESSAGE

    return PASSWORD_POLICY_MESSAGES.get(error_type, DEFAULT_PASSWORD_POLICY_MESSAGE)


def public_password_failure_message(error: object) -> str:
    if isinstance(error, PasswordPolicyException):
        return str(error.reason)

    reason = getattr(error, "reason", error)
    if isinstance(reason, str) and reason in PUBLIC_PASSWORD_POLICY_MESSAGES:
        return reason

    return DEFAULT_PASSWORD_POLICY_MESSAGE


def public_password_error_type(error: object) -> ResultErrorType:
    error_type = getattr(error, "error_type", None)
    if error_type in PASSWORD_POLICY_MESSAGES:
        return error_type

    return ERROR_INVALID_PASSWORD


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


def _is_identity_email_unique_violation(exc: IntegrityError) -> bool:
    constraint_name = getattr(
        getattr(exc.orig, "diag", None),
        "constraint_name",
        None,
    )
    if constraint_name and constraint_name in _IDENTITY_USER_EMAIL_UNIQUE_CONSTRAINTS:
        return True

    message = str(exc.orig).lower() if exc.orig else str(exc).lower()
    return "identity_user_email.email" in message and "unique" in message
