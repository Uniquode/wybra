from __future__ import annotations

from wybra.core.exceptions import ConfigurationError
from wybra.providers.settings import ProviderSettings, ProvidersSettings
from wybra.services.secrets import (
    KEYCHAIN_SOURCE,
    SecretsCapability,
    SecretsError,
    SecretValue,
)


class ProviderSecretResolutionError(ConfigurationError):
    """Raised when an enabled provider's secret lookup fails."""


def validate_provider_secret_settings(
    settings: ProvidersSettings,
    secrets: SecretsCapability | None,
) -> None:
    if settings.enabled_providers and secrets is None:
        raise ConfigurationError(
            "Enabled providers require SecretsCapability. Add `wybra.secrets` "
            "to the configured app modules or disable all providers."
        )
    for provider in settings.providers:
        reference = provider.required_client_secret_reference()
        if reference is None:
            continue
        source, key = reference
        assert secrets is not None
        try:
            secret_exists = secrets.exists(source, key)
        except SecretsError as exc:
            raise ProviderSecretResolutionError(
                f"Provider {provider.name!r} client secret validation failed: {exc}"
            ) from exc
        if not secret_exists:
            raise ProviderSecretResolutionError(
                f"Provider {provider.name!r} client secret is missing: "
                f"source={source}, key={key}."
            )


def resolve_provider_client_secret(
    settings: ProvidersSettings,
    provider_name: str,
    secrets: SecretsCapability,
) -> SecretValue:
    provider = settings.provider(provider_name)
    reference = provider.required_client_secret_reference()
    if reference is None:
        raise ConfigurationError(
            f"Provider {provider.name!r} does not configure a client secret reference."
        )
    source, key = reference
    try:
        return secrets.resolve(source, key)
    except SecretsError as exc:
        raise ProviderSecretResolutionError(
            f"Provider {provider.name!r} client secret resolution failed: {exc}"
        ) from exc


def provider_keychain_secret_references(
    providers: tuple[ProviderSettings, ...],
) -> tuple[tuple[str, str], ...]:
    references: list[tuple[str, str]] = []
    for provider in providers:
        reference = provider.required_client_secret_reference()
        if reference is None:
            continue
        source, key = reference
        if source == KEYCHAIN_SOURCE:
            references.append((provider.name, key))
    return tuple(references)


__all__ = (
    "ProviderSecretResolutionError",
    "provider_keychain_secret_references",
    "resolve_provider_client_secret",
    "validate_provider_secret_settings",
)
