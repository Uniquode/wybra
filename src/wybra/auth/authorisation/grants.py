"""Auth-backed effective scope grants for the core scope policy."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from wybra.auth.persistence.contracts import AuthPersistenceCapability
from wybra.scopes import ScopeSubject
from wybra.site import get_site


class AuthScopeGrantsProvider:
    """Resolve group-derived grants for an optionally authenticated user."""

    async def resolve_scope_subject(self, request: Request) -> ScopeSubject:
        from wybra.auth.capabilities import AuthCapability

        site = get_site(request.app)
        user = await site.require_capability(AuthCapability).optional_current_user(
            request
        )
        if user is None:
            return ScopeSubject()

        persistence = site.require_capability(AuthPersistenceCapability)
        async with persistence.scope() as scope:
            effective = await scope.authorisation.effective_scope_sets_for_user(user.id)
        return ScopeSubject(
            actor=user,
            granted_scopes=effective.scopes,
            groups=effective.groups,
        )


@dataclass(frozen=True, slots=True)
class AuthScopeCatalogueProvider:
    """Expose the auth scope catalogue through the core catalogue capability."""

    persistence: AuthPersistenceCapability

    async def list_scope_identifiers(self) -> tuple[str, ...]:
        async with self.persistence.scope() as scope:
            records = await scope.authorisation.list_scopes()
        return tuple(record.scope for record in records)


__all__ = ("AuthScopeCatalogueProvider", "AuthScopeGrantsProvider")
