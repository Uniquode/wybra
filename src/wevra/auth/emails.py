from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.email_normalisation import normalise_email_target
from wevra.auth.models import IdentityUserEmail, User


def email_lookup_statement(email: str) -> Select[tuple[User]]:
    normalized_email = normalise_email_target(email)
    if normalized_email is None:
        raise ValueError("invalid email address")

    return (
        select(User)
        .join(IdentityUserEmail)
        .where(IdentityUserEmail.email == normalized_email)
    )


async def resolve_user_by_email(
    session: AsyncSession,
    email: str,
) -> User | None:
    try:
        statement = email_lookup_statement(email)
    except ValueError:
        return None

    return (await session.execute(statement)).unique().scalar_one_or_none()
