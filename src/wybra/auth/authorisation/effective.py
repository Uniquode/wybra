from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.models import Group, GroupGroup, GroupScope, GroupUser, User
from wybra.auth.timestamps import current_timestamp


def is_user_effectively_active(user: User, *, now: float | None = None) -> bool:
    """Return whether the account is active at ``now``.

    ``expires_at`` is an exclusive upper bound: accounts are inactive at or
    after that Unix timestamp.
    """

    if not user.is_active:
        return False

    expires_at = user.expires_at
    if expires_at is None:
        return True

    reference_now = current_timestamp() if now is None else now
    return expires_at > reference_now


async def effective_scope_sets_for_user(
    session: AsyncSession,
    user_id: UUID,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    scopes, group_abbrevs = await _resolve_effective_scope_sets(session, user_id)
    return (tuple(sorted(scopes)), tuple(sorted(group_abbrevs)))


async def _resolve_effective_scope_sets(
    session: AsyncSession,
    user_id: UUID,
) -> tuple[set[str], set[str]]:
    direct_group_ids = set(
        (
            await session.execute(
                select(GroupUser.group_id).where(GroupUser.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    if not direct_group_ids:
        return set(), set()

    visited: set[UUID] = set()
    pending = set(direct_group_ids)

    while pending:
        current_ids = pending - visited
        if not current_ids:
            break
        visited.update(current_ids)
        child_ids = set(
            (
                await session.execute(
                    select(GroupGroup.child_group_id).where(
                        GroupGroup.parent_group_id.in_(current_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = child_ids - visited

    scopes = set(
        (
            await session.execute(
                select(GroupScope.scope).where(GroupScope.group_id.in_(visited))
            )
        )
        .scalars()
        .all()
    )
    group_abbrevs = set(
        (await session.execute(select(Group.abbrev).where(Group.id.in_(visited))))
        .scalars()
        .all()
    )

    return scopes, group_abbrevs


__all__ = [
    "effective_scope_sets_for_user",
    "is_user_effectively_active",
]
