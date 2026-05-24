from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from auth_ext.storage import ChallengeKind, ChallengeRecord, ChallengeStore


@dataclass(frozen=True, slots=True)
class PrimaryAuthenticationContext:
    user_id: str
    available_methods: tuple[ChallengeKind, ...] = ()


@dataclass(frozen=True, slots=True)
class ChallengeDecision:
    requires_challenge: bool
    challenge: ChallengeRecord | None = None


class AdvancedAuthenticationPolicy(Protocol):
    async def after_primary_authentication(
        self,
        context: PrimaryAuthenticationContext,
        challenge_store: ChallengeStore,
    ) -> ChallengeDecision: ...


class NoChallengePolicy:
    async def after_primary_authentication(
        self,
        context: PrimaryAuthenticationContext,
        challenge_store: ChallengeStore,
    ) -> ChallengeDecision:
        return ChallengeDecision(requires_challenge=False)


async def complete_challenge(
    challenge_store: ChallengeStore,
    challenge_id: str,
) -> bool:
    challenge = await challenge_store.get_challenge(challenge_id)
    if challenge is None:
        return False

    await challenge_store.consume_challenge(challenge_id)
    return True
