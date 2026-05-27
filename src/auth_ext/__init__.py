"""Reusable identity and authentication extension boundary for FastAPI Users."""

from auth_ext.bootstrap import (
    InitialAdminBootstrapResult,
    InitialAdminCredentials,
    bootstrap_initial_admin,
    find_administrative_user,
)
from auth_ext.challenges import (
    ChallengeDecision,
    ChallengeKind,
    ChallengeRecord,
    NoChallengePolicy,
    PrimaryAuthenticationContext,
    complete_challenge,
)
from auth_ext.configuration import ConfigurationError
from auth_ext.delivery import IdentityDelivery, NullIdentityDelivery
from auth_ext.manager import UserManager, create_password_helper, create_user_manager
from auth_ext.options import IdentityOptions, is_generate_local_identity_secret
from auth_ext.routing import RouteReplacement, RouterExtensionPlan
from auth_ext.schemas import UserCreate, UserRead, UserUpdate
from auth_ext.sessions import (
    authenticate_user,
    clear_session_cookie,
    create_authentication_backend,
    create_fastapi_users,
    create_session_token,
    destroy_session_token,
    optional_current_user,
    request_password_reset,
    request_verification,
    require_anonymous_user,
    require_current_user,
    reset_password,
    resolve_current_user,
    set_session_cookie,
    verify_user,
)
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
    "ConfigurationError",
    "IdentityDelivery",
    "IdentityOptions",
    "InitialAdminBootstrapResult",
    "InitialAdminCredentials",
    "NoChallengePolicy",
    "NullIdentityDelivery",
    "PrimaryAuthenticationContext",
    "RecoveryCodeStore",
    "RouteReplacement",
    "RouterExtensionPlan",
    "TOTPCredentialStore",
    "UserCreate",
    "UserManager",
    "UserRead",
    "UserUpdate",
    "WebAuthnCredentialStore",
    "authenticate_user",
    "bootstrap_initial_admin",
    "clear_session_cookie",
    "complete_challenge",
    "create_authentication_backend",
    "create_fastapi_users",
    "create_password_helper",
    "create_session_token",
    "create_user_manager",
    "destroy_session_token",
    "find_administrative_user",
    "is_generate_local_identity_secret",
    "optional_current_user",
    "request_password_reset",
    "request_verification",
    "require_anonymous_user",
    "require_current_user",
    "reset_password",
    "resolve_current_user",
    "set_session_cookie",
    "verify_user",
]
