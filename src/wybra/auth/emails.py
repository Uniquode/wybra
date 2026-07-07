from __future__ import annotations

from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.auth.models import IdentityUserEmail, User


async def resolve_user_by_normalised_email(
    connection: BaseDBAsyncClient,
    normalised_email: str,
) -> User | None:
    """Resolve a user by a pre-normalised email string."""
    email_record = await IdentityUserEmail.get_or_none(
        email=normalised_email,
        using_db=connection,
    )
    if email_record is None:
        return None
    return await User.get_or_none(id=email_record.user_id, using_db=connection)
