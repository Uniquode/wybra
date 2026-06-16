from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol

from wybra.auth.mfa.storage import ChallengeKind, ChallengeRecord, ChallengeStore
from wybra.auth.timestamps import current_timestamp

AuthenticationMethod = Literal["primary", "totp", "webauthn"]
AUTHENTICATION_ASSERTION_MAX_AGE_SECONDS: Final[float] = 300.0
PRIMARY_ASSERTION_METHOD: Final[AuthenticationMethod] = "primary"
TOTP_ASSERTION_METHOD: Final[AuthenticationMethod] = "totp"
WEBAUTHN_ASSERTION_METHOD: Final[AuthenticationMethod] = "webauthn"


@dataclass(frozen=True, slots=True)
class PrimaryAuthenticationContext:
    user_id: str
    available_methods: tuple[ChallengeKind, ...] = ()
    required_methods: tuple[AuthenticationMethod, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthenticationAssertion:
    user_id: str
    method: AuthenticationMethod
    asserted_at: float
    ceremony_id: str


def required_authentication_methods_for_totp_policy(
    *,
    totp_enabled: bool,
    has_active_totp: bool,
) -> tuple[AuthenticationMethod, ...]:
    if totp_enabled and has_active_totp:
        return (TOTP_ASSERTION_METHOD,)

    return ()


def assertions_satisfy_required_methods(
    *,
    user_id: str,
    ceremony_id: str,
    required_methods: tuple[AuthenticationMethod, ...],
    assertions: tuple[AuthenticationAssertion, ...],
    now: float | None = None,
    max_age_seconds: float = AUTHENTICATION_ASSERTION_MAX_AGE_SECONDS,
) -> bool:
    comparison_time = current_timestamp() if now is None else now
    asserted_methods = {
        assertion.method
        for assertion in assertions
        if assertion.user_id == user_id
        and assertion.ceremony_id == ceremony_id
        and 0 <= comparison_time - assertion.asserted_at <= max_age_seconds
    }
    return set(required_methods).issubset(asserted_methods)


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
