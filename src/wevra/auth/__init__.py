"""Reusable identity and authentication infrastructure for FastAPI Users."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AccountCreationPolicy": "wevra.auth.options",
    "AdvancedAuthenticationPolicy": "wevra.auth.mfa.challenges",
    "AuthenticationAssertion": "wevra.auth.mfa.challenges",
    "AuthenticationMethod": "wevra.auth.mfa.challenges",
    "AUTH_SETTINGS_OWNER": "wevra.auth.settings",
    "AuthCapability": "wevra.auth.capabilities",
    "AuthSettings": "wevra.auth.settings",
    "ChallengeDecision": "wevra.auth.mfa.challenges",
    "ChallengeKind": "wevra.auth.mfa.challenges",
    "ChallengeRecord": "wevra.auth.mfa.challenges",
    "ChallengeStore": "wevra.auth.mfa.storage",
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
    "ProviderCredentialSecrets": "wevra.auth.provider_credentials",
    "ProfileImage": "wevra.auth.profile",
    "ProfileUser": "wevra.auth.profile",
    "TOTP_ASSERTION_METHOD": "wevra.auth.mfa.challenges",
    "RecoveryCodeStore": "wevra.auth.mfa.storage",
    "Result": "wevra.auth.result",
    "ResultErrorType": "wevra.auth.result",
    "ResultValue": "wevra.auth.result",
    "RouteReplacement": "wevra.auth.routes.wiring",
    "RouterExtensionPlan": "wevra.auth.routes.wiring",
    "SqlAlchemyProviderCredentialStore": "wevra.auth.provider_credentials",
    "SiteAuthCapability": "wevra.auth.capabilities",
    "TOTPCredentialStore": "wevra.auth.mfa.storage",
    "UserCreate": "wevra.auth.accounts.schemas",
    "UserRead": "wevra.auth.accounts.schemas",
    "UserUpdate": "wevra.auth.accounts.schemas",
    "WebAuthnCredentialStore": "wevra.auth.mfa.storage",
    "complete_challenge": "wevra.auth.mfa.challenges",
    "is_generate_local_identity_secret": "wevra.auth.options",
    "auth_settings_from_state": "wevra.auth.settings",
    "anonymous_required": "wevra.auth.capabilities",
    "identity_options_from_state": "wevra.auth.settings",
    "login_required": "wevra.auth.capabilities",
    "load_auth_settings": "wevra.auth.settings",
    "load_auth_settings_from_config": "wevra.auth.settings",
    "load_runtime_auth_settings": "wevra.auth.settings",
    "required_authentication_methods_for_totp_policy": "wevra.auth.mfa.challenges",
    "optional_current_user": "wevra.auth.capabilities",
    "profile_image_for_user": "wevra.auth.profile",
    "setup_site": "wevra.auth.capabilities",
    "validate_auth_settings": "wevra.auth.settings",
    "supported_auth_environment_names": "wevra.auth.settings",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
