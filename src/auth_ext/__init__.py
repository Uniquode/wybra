"""Reusable identity and authentication extension boundary for FastAPI Users."""

from auth_ext.challenges import (
    AdvancedAuthenticationPolicy,
    ChallengeDecision,
    ChallengeKind,
    ChallengeRecord,
    NoChallengePolicy,
    PrimaryAuthenticationContext,
    complete_challenge,
)
from auth_ext.configuration import ConfigurationError
from auth_ext.delivery import IdentityDelivery, NullIdentityDelivery
from auth_ext.options import (
    AccountCreationPolicy,
    IdentityIntegration,
    IdentityOptions,
    is_generate_local_identity_secret,
)
from auth_ext.routing import RouteReplacement, RouterExtensionPlan
from auth_ext.schemas import UserCreate, UserRead, UserUpdate
from auth_ext.storage import (
    ChallengeStore,
    RecoveryCodeStore,
    TOTPCredentialStore,
    WebAuthnCredentialStore,
)

__all__ = [
    "AccountCreationPolicy",
    "AdvancedAuthenticationPolicy",
    "ChallengeDecision",
    "ChallengeKind",
    "ChallengeRecord",
    "ChallengeStore",
    "ConfigurationError",
    "IdentityDelivery",
    "IdentityIntegration",
    "IdentityOptions",
    "NoChallengePolicy",
    "NullIdentityDelivery",
    "PrimaryAuthenticationContext",
    "RecoveryCodeStore",
    "TOTPCredentialStore",
    "UserCreate",
    "UserRead",
    "UserUpdate",
    "WebAuthnCredentialStore",
    "complete_challenge",
    "is_generate_local_identity_secret",
    "RouteReplacement",
    "RouterExtensionPlan",
]
