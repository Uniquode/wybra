"""Advanced authentication extension boundary for FastAPI Users."""

from auth_ext.challenges import (
    ChallengeDecision,
    ChallengeKind,
    ChallengeRecord,
    NoChallengePolicy,
    PrimaryAuthenticationContext,
    complete_challenge,
)
from auth_ext.routing import RouteReplacement, RouterExtensionPlan
from auth_ext.storage import (
    ChallengeStore,
    RecoveryCodeStore,
    TOTPCredentialStore,
    WebAuthnCredentialStore,
)

__all__ = [
    "ChallengeDecision",
    "ChallengeKind",
    "ChallengeRecord",
    "ChallengeStore",
    "NoChallengePolicy",
    "PrimaryAuthenticationContext",
    "RecoveryCodeStore",
    "RouteReplacement",
    "RouterExtensionPlan",
    "TOTPCredentialStore",
    "WebAuthnCredentialStore",
    "complete_challenge",
]
