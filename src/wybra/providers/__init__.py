"""Opt-in external identity provider integration."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "APPLE_PROVIDER_NAME": "wybra.providers.apple",
    "GITHUB_PROVIDER_NAME": "wybra.providers.github",
    "GOOGLE_PROVIDER_NAME": "wybra.providers.google",
    "PROVIDERS_CONFIG_SECTION": "wybra.providers.settings",
    "ProviderAccountResolution": "wybra.providers.account_resolution",
    "ProviderAccountPolicy": "wybra.providers.policy",
    "ProviderAssertion": "wybra.providers.policy",
    "ProviderAuthDescriptor": "wybra.providers.descriptors",
    "ProviderPolicyDecision": "wybra.providers.policy",
    "ProviderPolicyOutcome": "wybra.providers.policy",
    "ProviderSecretAvailabilityIssue": "wybra.providers.secrets",
    "ProviderSecretResolutionError": "wybra.providers.secrets",
    "ProviderSettings": "wybra.providers.settings",
    "ProvidersCapability": "wybra.providers.capabilities",
    "ProvidersSettings": "wybra.providers.settings",
    "SiteProvidersCapability": "wybra.providers.capabilities",
    "AppleClientSecretError": "wybra.providers.apple",
    "AppleIDTokenClaims": "wybra.providers.apple",
    "AppleIDTokenValidationError": "wybra.providers.apple",
    "AppleIDTokenValidationRequest": "wybra.providers.apple",
    "AppleIDTokenValidator": "wybra.providers.apple",
    "AppleOIDCIDTokenValidator": "wybra.providers.apple",
    "AppleOAuthSettings": "wybra.providers.apple",
    "AppleOAuthTokenClient": "wybra.providers.apple",
    "AppleTokenClient": "wybra.providers.apple",
    "AppleTokenExchangeError": "wybra.providers.apple",
    "AppleTokenExchangeRequest": "wybra.providers.apple",
    "AppleTokenResponse": "wybra.providers.apple",
    "GitHubAPIClient": "wybra.providers.github",
    "GitHubAPIError": "wybra.providers.github",
    "GitHubIdentityRequest": "wybra.providers.github",
    "GitHubOAuthSettings": "wybra.providers.github",
    "GitHubOAuthTokenClient": "wybra.providers.github",
    "GitHubRESTAPIClient": "wybra.providers.github",
    "GitHubTokenClient": "wybra.providers.github",
    "GitHubTokenExchangeError": "wybra.providers.github",
    "GitHubTokenExchangeRequest": "wybra.providers.github",
    "GitHubTokenResponse": "wybra.providers.github",
    "GitHubUserClaims": "wybra.providers.github",
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
    "apple_id_token_claims_from_payload": "wybra.providers.apple",
    "apple_oauth_settings_from_provider": "wybra.providers.apple",
    "apple_token_response_from_payload": "wybra.providers.apple",
    "create_apple_client_secret": "wybra.providers.apple",
    "github_oauth_settings_from_provider": "wybra.providers.github",
    "github_token_response_from_payload": "wybra.providers.github",
    "github_user_claims_from_api_payloads": "wybra.providers.github",
    "google_id_token_claims_from_payload": "wybra.providers.google",
    "google_oauth_settings_from_provider": "wybra.providers.google",
    "google_token_response_from_payload": "wybra.providers.google",
    "post_setup_site": "wybra.providers.capabilities",
    "provider_auth_descriptor": "wybra.providers.descriptors",
    "provider_auth_descriptors": "wybra.providers.descriptors",
    "provider_keychain_secret_references": "wybra.providers.secrets",
    "provider_label": "wybra.providers.descriptors",
    "provider_name_value": "wybra.providers.settings",
    "provider_settings_with_available_secrets": "wybra.providers.secrets",
    "provider_settings_from_config": "wybra.providers.settings",
    "resolve_provider_client_secret": "wybra.providers.secrets",
    "resolve_provider_account": "wybra.providers.account_resolution",
    "resolve_provider_private_key": "wybra.providers.secrets",
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
