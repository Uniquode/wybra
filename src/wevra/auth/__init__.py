"""Reusable identity and authentication infrastructure for FastAPI Users."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AccountCreationPolicy": "wevra.auth.options",
    "AdvancedAuthenticationPolicy": "wevra.auth.mfa.challenges",
    "AuthenticationAssertion": "wevra.auth.mfa.challenges",
    "AuthenticationMethod": "wevra.auth.mfa.challenges",
    "ChallengeDecision": "wevra.auth.mfa.challenges",
    "ChallengeKind": "wevra.auth.mfa.challenges",
    "ChallengeRecord": "wevra.auth.mfa.challenges",
    "ChallengeStore": "wevra.auth.mfa.storage",
    "ConfigurationError": "wevra.auth.configuration",
    "DefaultPasswordPolicy": "wevra.auth.accounts.passwords",
    "ERROR_ALREADY_EXISTS": "wevra.auth.result",
    "ERROR_ALREADY_VERIFIED": "wevra.auth.result",
    "ERROR_AUTHENTICATION_METHOD_REQUIRED": "wevra.auth.result",
    "ERROR_IDENTITY_CHANGED": "wevra.auth.result",
    "ERROR_INACTIVE_USER": "wevra.auth.result",
    "ERROR_INVALID_EMAIL": "wevra.auth.result",
    "ERROR_INVALID_TOKEN": "wevra.auth.result",
    "ERROR_INVALID_PASSWORD": "wevra.auth.result",
    "ERROR_PASSWORD_TOO_SHORT": "wevra.auth.result",
    "ERROR_PASSWORD_TOO_WEAK": "wevra.auth.result",
    "ERROR_POLICY_DISABLED": "wevra.auth.result",
    "ERROR_TOTP_CODE_REQUIRED": "wevra.auth.result",
    "ERROR_TOTP_INVALID": "wevra.auth.result",
    "ERROR_TOTP_RECOVERY_INVALID": "wevra.auth.result",
    "ERROR_TOTP_SETUP_REQUIRED": "wevra.auth.result",
    "ERROR_TOKEN_REJECTED": "wevra.auth.result",
    "IdentityDelivery": "wevra.auth.delivery",
    "IdentityIntegration": "wevra.auth.options",
    "IdentityOptions": "wevra.auth.options",
    "NoChallengePolicy": "wevra.auth.mfa.challenges",
    "NullIdentityDelivery": "wevra.auth.delivery",
    "PasswordPolicy": "wevra.auth.accounts.passwords",
    "PasswordStrength": "wevra.auth.accounts.passwords",
    "PasswordStrengthLabel": "wevra.auth.accounts.passwords",
    "PrimaryAuthenticationContext": "wevra.auth.mfa.challenges",
    "TOTP_ASSERTION_METHOD": "wevra.auth.mfa.challenges",
    "RecoveryCodeStore": "wevra.auth.mfa.storage",
    "Result": "wevra.auth.result",
    "ResultErrorType": "wevra.auth.result",
    "ResultValue": "wevra.auth.result",
    "RouteReplacement": "wevra.auth.routes.wiring",
    "RouterExtensionPlan": "wevra.auth.routes.wiring",
    "TOTPCredentialStore": "wevra.auth.mfa.storage",
    "UserCreate": "wevra.auth.accounts.schemas",
    "UserRead": "wevra.auth.accounts.schemas",
    "UserUpdate": "wevra.auth.accounts.schemas",
    "WebAuthnCredentialStore": "wevra.auth.mfa.storage",
    "complete_challenge": "wevra.auth.mfa.challenges",
    "is_generate_local_identity_secret": "wevra.auth.options",
    "required_authentication_methods_for_totp_policy": "wevra.auth.mfa.challenges",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
