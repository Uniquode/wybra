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
from auth_ext.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_ALREADY_VERIFIED,
    ERROR_IDENTITY_CHANGED,
    ERROR_INACTIVE_USER,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    ERROR_INVALID_TOKEN,
    ERROR_POLICY_DISABLED,
    ERROR_TOKEN_REJECTED,
    Result,
    ResultErrorType,
    ResultValue,
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
    "ERROR_ALREADY_EXISTS",
    "ERROR_ALREADY_VERIFIED",
    "ERROR_IDENTITY_CHANGED",
    "ERROR_INACTIVE_USER",
    "ERROR_INVALID_EMAIL",
    "ERROR_INVALID_TOKEN",
    "ERROR_INVALID_PASSWORD",
    "ERROR_POLICY_DISABLED",
    "ERROR_TOKEN_REJECTED",
    "IdentityDelivery",
    "IdentityIntegration",
    "IdentityOptions",
    "NoChallengePolicy",
    "NullIdentityDelivery",
    "PrimaryAuthenticationContext",
    "RecoveryCodeStore",
    "Result",
    "ResultErrorType",
    "ResultValue",
    "RouteReplacement",
    "RouterExtensionPlan",
    "TOTPCredentialStore",
    "UserCreate",
    "UserRead",
    "UserUpdate",
    "WebAuthnCredentialStore",
    "complete_challenge",
    "is_generate_local_identity_secret",
]
