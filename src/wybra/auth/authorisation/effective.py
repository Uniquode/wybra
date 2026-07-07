from __future__ import annotations

from typing import cast
from uuid import UUID

from tortoise.backends.base.client import BaseDBAsyncClient

from wybra.auth.models import Group, GroupGroup, GroupScope, GroupUser
from wybra.auth.persistence.contracts import LocalUserRecord
from wybra.auth.timestamps import current_timestamp


def is_user_effectively_active(
    user: LocalUserRecord,
    *,
    now: float | None = None,
) -> bool:
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
    connection: BaseDBAsyncClient,
    user_id: UUID,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    scopes, group_abbrevs = await _resolve_effective_scope_sets(connection, user_id)
    return (tuple(sorted(scopes)), tuple(sorted(group_abbrevs)))


async def _resolve_effective_scope_sets(
    connection: BaseDBAsyncClient,
    user_id: UUID,
) -> tuple[set[str], set[str]]:
    direct_group_ids = cast(
        set[UUID],
        set(
            await GroupUser.filter(user_id=user_id)
            .using_db(connection)
            .values_list("group_id", flat=True)
        ),
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
        child_ids = cast(
            set[UUID],
            set(
                await GroupGroup.filter(parent_group_id__in=current_ids)
                .using_db(connection)
                .values_list("child_group_id", flat=True)
            ),
        )
        pending = child_ids - visited

    scopes = cast(
        set[str],
        set(
            await GroupScope.filter(group_id__in=visited)
            .using_db(connection)
            .values_list("scope", flat=True)
        ),
    )
    group_abbrevs = cast(
        set[str],
        set(
            await Group.filter(id__in=visited)
            .using_db(connection)
            .values_list("abbrev", flat=True)
        ),
    )

    return scopes, group_abbrevs


__all__ = [
    "effective_scope_sets_for_user",
    "is_user_effectively_active",
]
