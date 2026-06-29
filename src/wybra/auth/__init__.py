"""Reusable identity and authentication infrastructure for FastAPI Users."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AccountCreationPolicy": "wybra.auth.options",
    "AdvancedAuthenticationPolicy": "wybra.auth.mfa.challenges",
    "AuthenticationAssertion": "wybra.auth.mfa.challenges",
    "AuthenticationMethod": "wybra.auth.mfa.challenges",
    "AUTH_SETTINGS_OWNER": "wybra.auth.settings",
    "AuthCapability": "wybra.auth.capabilities",
    "AuthSettings": "wybra.auth.settings",
    "ChallengeDecision": "wybra.auth.mfa.challenges",
    "ChallengeKind": "wybra.auth.mfa.challenges",
    "ChallengeRecord": "wybra.auth.mfa.challenges",
    "ChallengeStore": "wybra.auth.mfa.storage",
    "DefaultPasswordPolicy": "wybra.auth.accounts.passwords",
    "ERROR_ALREADY_EXISTS": "wybra.auth.result",
    "ERROR_ALREADY_VERIFIED": "wybra.auth.result",
    "ERROR_AUTHENTICATION_METHOD_REQUIRED": "wybra.auth.result",
    "ERROR_IDENTITY_CHANGED": "wybra.auth.result",
    "ERROR_INACTIVE_USER": "wybra.auth.result",
    "ERROR_INVALID_EMAIL": "wybra.auth.result",
    "ERROR_INVALID_TOKEN": "wybra.auth.result",
    "ERROR_INVALID_PASSWORD": "wybra.auth.result",
    "ERROR_PASSWORD_TOO_SHORT": "wybra.auth.result",
    "ERROR_PASSWORD_TOO_WEAK": "wybra.auth.result",
    "ERROR_POLICY_DISABLED": "wybra.auth.result",
    "ERROR_TOTP_CODE_REQUIRED": "wybra.auth.result",
    "ERROR_TOTP_INVALID": "wybra.auth.result",
    "ERROR_TOTP_RECOVERY_INVALID": "wybra.auth.result",
    "ERROR_TOTP_SETUP_REQUIRED": "wybra.auth.result",
    "ERROR_TOKEN_REJECTED": "wybra.auth.result",
    "IdentityDelivery": "wybra.auth.delivery",
    "IdentityIntegration": "wybra.auth.options",
    "IdentityOptions": "wybra.auth.options",
    "NoChallengePolicy": "wybra.auth.mfa.challenges",
    "NullIdentityDelivery": "wybra.auth.delivery",
    "PasswordPolicy": "wybra.auth.accounts.passwords",
    "PasswordStrength": "wybra.auth.accounts.passwords",
    "PasswordStrengthLabel": "wybra.auth.accounts.passwords",
    "PrimaryAuthenticationContext": "wybra.auth.mfa.challenges",
    "ProviderCredentialSecrets": "wybra.auth.provider_credentials",
    "TOTP_ASSERTION_METHOD": "wybra.auth.mfa.challenges",
    "RecoveryCodeStore": "wybra.auth.mfa.storage",
    "Result": "wybra.auth.result",
    "ResultErrorType": "wybra.auth.result",
    "ResultValue": "wybra.auth.result",
    "RouteReplacement": "wybra.auth.routes.wiring",
    "RouterExtensionPlan": "wybra.auth.routes.wiring",
    "SqlAlchemyProviderCredentialStore": "wybra.auth.provider_credentials",
    "SiteAuthCapability": "wybra.auth.capabilities",
    "TOTPCredentialStore": "wybra.auth.mfa.storage",
    "UserCreate": "wybra.auth.accounts.schemas",
    "UserRead": "wybra.auth.accounts.schemas",
    "UserUpdate": "wybra.auth.accounts.schemas",
    "WebAuthnCredentialStore": "wybra.auth.mfa.storage",
    "complete_challenge": "wybra.auth.mfa.challenges",
    "is_generate_local_identity_secret": "wybra.auth.options",
    "auth_settings_from_state": "wybra.auth.settings",
    "anonymous_required": "wybra.auth.capabilities",
    "identity_options_from_state": "wybra.auth.settings",
    "login_required": "wybra.auth.capabilities",
    "load_auth_settings": "wybra.auth.settings",
    "load_runtime_auth_settings": "wybra.auth.settings",
    "module_config": "wybra.auth.settings",
    "required_authentication_methods_for_totp_policy": "wybra.auth.mfa.challenges",
    "optional_current_user": "wybra.auth.capabilities",
    "post_setup_site": "wybra.auth.capabilities",
    "setup_site": "wybra.auth.capabilities",
    "validate_auth_settings": "wybra.auth.settings",
    "supported_auth_environment_names": "wybra.auth.settings",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
