from __future__ import annotations

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.models import IdentityUserEmail, User

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def normalise_email_target(target: str) -> str | None:
    """Return a canonical lowercase email key for identity lookup."""
    try:
        return str(_EMAIL_ADAPTER.validate_python(target)).casefold()
    except ValidationError:
        return None


def email_lookup_statement(email: str) -> Select[tuple[User]]:
    normalized_email = normalise_email_target(email)
    if normalized_email is None:
        raise ValueError("invalid email address")

    return (
        select(User)
        .join(IdentityUserEmail)
        .where(func.lower(IdentityUserEmail.email) == normalized_email)
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
