"""Opt-in external identity provider integration."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "PROVIDERS_CONFIG_SECTION": "wybra.providers.settings",
    "ProviderAccountPolicy": "wybra.providers.policy",
    "ProviderAssertion": "wybra.providers.policy",
    "ProviderPolicyDecision": "wybra.providers.policy",
    "ProviderPolicyOutcome": "wybra.providers.policy",
    "ProviderSecretAvailabilityIssue": "wybra.providers.secrets",
    "ProviderSecretResolutionError": "wybra.providers.secrets",
    "ProviderSettings": "wybra.providers.settings",
    "ProvidersCapability": "wybra.providers.capabilities",
    "ProvidersSettings": "wybra.providers.settings",
    "SiteProvidersCapability": "wybra.providers.capabilities",
    "GoogleIDTokenClaims": "wybra.providers.google",
    "GoogleIDTokenValidationError": "wybra.providers.google",
    "GoogleIDTokenValidationRequest": "wybra.providers.google",
    "GoogleIDTokenValidator": "wybra.providers.google",
    "GoogleOIDCIDTokenValidator": "wybra.providers.google",
    "GoogleOAuthTokenClient": "wybra.providers.google",
    "GoogleOAuthSettings": "wybra.providers.google",
    "GoogleTokenClient": "wybra.providers.google",
    "GoogleTokenExchangeError": "wybra.providers.google",
    "GoogleTokenExchangeRequest": "wybra.providers.google",
    "GoogleTokenResponse": "wybra.providers.google",
    "module_config": "wybra.providers.settings",
    "google_id_token_claims_from_payload": "wybra.providers.google",
    "google_oauth_settings_from_provider": "wybra.providers.google",
    "google_token_response_from_payload": "wybra.providers.google",
    "post_setup_site": "wybra.providers.capabilities",
    "provider_keychain_secret_references": "wybra.providers.secrets",
    "provider_name_value": "wybra.providers.settings",
    "provider_settings_with_available_secrets": "wybra.providers.secrets",
    "provider_settings_from_config": "wybra.providers.settings",
    "resolve_provider_client_secret": "wybra.providers.secrets",
    "setup_site": "wybra.providers.capabilities",
    "validate_provider_configuration": "wybra.providers.validation",
    "validate_provider_secret_settings": "wybra.providers.secrets",
    "validation_targets": "wybra.providers.validation",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
