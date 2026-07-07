import asyncio
from dataclasses import dataclass

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError

from wybra.auth.accounts.manager import create_user_manager
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.delivery import IdentityDelivery
from wybra.auth.models import InitialAdminBootstrap, User
from wybra.auth.options import IdentityOptions
from wybra.auth.persistence.contracts import LocalUserRecord
from wybra.auth.persistence.transactions import auth_savepoint

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
    user: LocalUserRecord


async def find_administrative_user(connection: BaseDBAsyncClient) -> User | None:
    return await User.filter(is_superuser=True).using_db(connection).first()


async def bootstrap_initial_admin(
    connection: BaseDBAsyncClient,
    options: IdentityOptions,
    credentials: InitialAdminCredentials,
    delivery: IdentityDelivery | None = None,
) -> InitialAdminBootstrapResult:
    existing_admin = await find_administrative_user(connection)
    if existing_admin is not None:
        return InitialAdminBootstrapResult(created=False, user=existing_admin)

    if not await _claim_initial_admin_bootstrap(connection):
        existing_admin = await _wait_for_claimed_administrative_user(connection)
        if existing_admin is None:
            raise RuntimeError(
                "Initial admin bootstrap was already claimed, but no "
                "administrative user exists."
            )

        return InitialAdminBootstrapResult(created=False, user=existing_admin)

    manager = create_user_manager(connection, options, delivery)
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


async def _claim_initial_admin_bootstrap(connection: BaseDBAsyncClient) -> bool:
    """Atomically claim the initial-admin bootstrap slot.

    The fixed primary key is the cross-process lock. Concurrent bootstrap
    attempts cannot both insert it, so an IntegrityError means another writer
    already claimed the slot and this caller should wait for that admin user.
    """
    try:
        async with auth_savepoint(connection) as savepoint:
            await InitialAdminBootstrap.create(
                id=_INITIAL_ADMIN_BOOTSTRAP_ID,
                using_db=savepoint,
            )
    except IntegrityError:
        return False

    return True


async def _wait_for_claimed_administrative_user(
    connection: BaseDBAsyncClient,
) -> User | None:
    for _ in range(_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_ATTEMPTS):
        existing_admin = await find_administrative_user(connection)
        if existing_admin is not None:
            return existing_admin

        await asyncio.sleep(_INITIAL_ADMIN_BOOTSTRAP_LOOKUP_DELAY_SECONDS)

    return None
