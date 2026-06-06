from typing import Protocol

from fastapi import Request

from wevra.auth.models import User


class IdentityDelivery(Protocol):
    async def send_reset_password_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None: ...

    async def send_verification_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None: ...


class NullIdentityDelivery:
    async def send_reset_password_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        return None

    async def send_verification_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        return None
