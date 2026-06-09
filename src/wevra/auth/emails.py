from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.email_normalisation import normalise_email_target
from wevra.auth.models import IdentityUserEmail, User


def email_lookup_statement(email: str) -> Select[tuple[User]]:
    """Build an email lookup statement from a raw email value.

    Raises:
        ValueError: If the email cannot be normalised.
    """

    normalised_email = normalise_email_target(email)
    if normalised_email is None:
        raise ValueError("invalid email address")
    return email_lookup_statement_for_normalised_email(normalised_email)


def email_lookup_statement_for_normalised_email(
    normalised_email: str,
) -> Select[tuple[User]]:
    """Build an email lookup statement from a normalised email value."""

    return (
        select(User)
        .join(IdentityUserEmail)
        .where(IdentityUserEmail.email == normalised_email)
    )


async def resolve_user_by_email(
    session: AsyncSession,
    email: str,
) -> User | None:
    """Resolve a user by a raw email string."""
    try:
        statement = email_lookup_statement(email)
    except ValueError:
        return None

    return (await session.execute(statement)).unique().scalar_one_or_none()


async def resolve_user_by_normalised_email(
    session: AsyncSession,
    normalised_email: str,
) -> User | None:
    """Resolve a user by a pre-normalised email string."""
    return (
        (
            await session.execute(
                email_lookup_statement_for_normalised_email(normalised_email)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
