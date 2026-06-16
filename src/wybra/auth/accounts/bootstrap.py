import asyncio
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.accounts.manager import create_user_manager
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.delivery import IdentityDelivery
from wybra.auth.models import InitialAdminBootstrap, User
from wybra.auth.options import IdentityOptions

_INITIAL_ADMIN_BOOTSTRAP_ID = 1
_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_ATTEMPTS = 10
_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_DELAY_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class InitialAdminCredentials:
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class InitialAdminBootstrapResult:
    created: bool
    user: User


async def find_administrative_user(session: AsyncSession) -> User | None:
    is_superuser = cast(Any, User.is_superuser)
    result = await session.execute(select(User).where(is_superuser.is_(True)).limit(1))
    return result.scalar_one_or_none()


async def bootstrap_initial_admin(
    session: AsyncSession,
    options: IdentityOptions,
    credentials: InitialAdminCredentials,
    delivery: IdentityDelivery | None = None,
) -> InitialAdminBootstrapResult:
    existing_admin = await find_administrative_user(session)
    if existing_admin is not None:
        return InitialAdminBootstrapResult(created=False, user=existing_admin)

    if not await _claim_initial_admin_bootstrap(session):
        existing_admin = await _wait_for_claimed_administrative_user(session)
        if existing_admin is None:
            raise RuntimeError(
                "Initial admin bootstrap was already claimed, but no "
                "administrative user exists."
            )

        return InitialAdminBootstrapResult(created=False, user=existing_admin)

    manager = create_user_manager(session, options, delivery)
    user = await manager.create(
        UserCreate(
            email=credentials.email,
            password=credentials.password,
            is_superuser=True,
            is_verified=True,
        ),
        safe=False,
    )
    return InitialAdminBootstrapResult(created=True, user=user)


async def _claim_initial_admin_bootstrap(session: AsyncSession) -> bool:
    """Atomically claim the initial-admin bootstrap slot.

    The fixed primary key is the cross-process lock. Concurrent bootstrap
    attempts cannot both insert it, so an IntegrityError means another writer
    already claimed the slot and this caller should wait for that admin user.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                insert(InitialAdminBootstrap).values(
                    id=_INITIAL_ADMIN_BOOTSTRAP_ID,
                )
            )
    except IntegrityError:
        await session.rollback()
        return False

    return True


async def _wait_for_claimed_administrative_user(session: AsyncSession) -> User | None:
    for _ in range(_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_ATTEMPTS):
        existing_admin = await find_administrative_user(session)
        if existing_admin is not None:
            return existing_admin

        await asyncio.sleep(_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_DELAY_SECONDS)

    return None
