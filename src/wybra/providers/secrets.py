from __future__ import annotations

from dataclasses import dataclass, replace

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


@dataclass(frozen=True, slots=True)
class ProviderSecretAvailabilityIssue:
    provider_name: str
    message: str


def provider_settings_with_available_secrets(
    settings: ProvidersSettings,
    secrets: SecretsCapability | None,
) -> tuple[ProvidersSettings, tuple[ProviderSecretAvailabilityIssue, ...]]:
    providers: list[ProviderSettings] = []
    issues: list[ProviderSecretAvailabilityIssue] = []
    for provider in settings.providers:
        issue = _provider_secret_availability_issue(provider, secrets)
        if issue is None:
            providers.append(provider)
            continue
        issues.append(issue)
        providers.append(replace(provider, enabled=False))
    return ProvidersSettings(providers=tuple(providers)), tuple(issues)


def _provider_secret_availability_issue(
    provider: ProviderSettings,
    secrets: SecretsCapability | None,
) -> ProviderSecretAvailabilityIssue | None:
    if not provider.enabled:
        return None

    reference = provider.required_provider_secret_reference()
    if reference is None:
        return None

    source, key, secret_label = reference
    if secrets is None:
        return ProviderSecretAvailabilityIssue(
            provider_name=provider.name,
            message=(
                f"{secret_label} cannot be checked because SecretsCapability is "
                "not available; add `wybra.secrets` to app modules or disable "
                "the provider"
            ),
        )

    try:
        secret_exists = secrets.exists(source, key)
    except SecretsError as exc:
        return ProviderSecretAvailabilityIssue(
            provider_name=provider.name,
            message=f"{secret_label} validation failed: {exc}",
        )

    if not secret_exists:
        return ProviderSecretAvailabilityIssue(
            provider_name=provider.name,
            message=f"{secret_label} is missing: source={source}, key={key}",
        )

    return None


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
        reference = provider.required_provider_secret_reference()
        if reference is None:
            continue
        source, key, secret_label = reference
        assert secrets is not None
        try:
            secret_exists = secrets.exists(source, key)
        except SecretsError as exc:
            raise ProviderSecretResolutionError(
                f"Provider {provider.name!r} {secret_label} validation failed: {exc}"
            ) from exc
        if not secret_exists:
            raise ProviderSecretResolutionError(
                f"Provider {provider.name!r} {secret_label} is missing: "
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


def resolve_provider_private_key(
    settings: ProvidersSettings,
    provider_name: str,
    secrets: SecretsCapability,
) -> SecretValue:
    provider = settings.provider(provider_name)
    reference = provider.required_private_key_reference()
    if reference is None:
        raise ConfigurationError(
            f"Provider {provider.name!r} does not configure a private key reference."
        )
    source, key = reference
    try:
        return secrets.resolve(source, key)
    except SecretsError as exc:
        raise ProviderSecretResolutionError(
            f"Provider {provider.name!r} private key resolution failed: {exc}"
        ) from exc


def provider_keychain_secret_references(
    providers: tuple[ProviderSettings, ...],
) -> tuple[tuple[str, str], ...]:
    references: list[tuple[str, str]] = []
    for provider in providers:
        reference = provider.required_provider_secret_reference()
        if reference is None:
            continue
        source, key, _secret_label = reference
        if source == KEYCHAIN_SOURCE:
            references.append((provider.name, key))
    return tuple(references)


__all__ = (
    "ProviderSecretAvailabilityIssue",
    "ProviderSecretResolutionError",
    "provider_keychain_secret_references",
    "provider_settings_with_available_secrets",
    "resolve_provider_client_secret",
    "resolve_provider_private_key",
    "validate_provider_secret_settings",
)
