from __future__ import annotations

import logging
import uuid

from fastapi import Request
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users.exceptions import InvalidPasswordException
from fastapi_users.password import PasswordHelper
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.accounts.schemas import UserCreate
from wevra.auth.delivery import IdentityDelivery, NullIdentityDelivery
from wevra.auth.models import User
from wevra.auth.options import IdentityOptions
from wevra.auth.persistence import create_user_database
from wevra.auth.result import (
    ERROR_INVALID_PASSWORD,
    ERROR_PASSWORD_TOO_SHORT,
    ERROR_PASSWORD_TOO_WEAK,
    ResultErrorType,
)

logger = logging.getLogger(__name__)
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
