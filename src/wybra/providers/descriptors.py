from collections.abc import Callable
from dataclasses import dataclass

from wybra.core.exceptions import ConfigurationError
from wybra.providers.apple import (
    APPLE_PROVIDER_NAME,
    apple_oauth_settings_from_provider,
)
from wybra.providers.github import (
    GITHUB_PROVIDER_NAME,
    github_oauth_settings_from_provider,
)
from wybra.providers.google import (
    GOOGLE_PROVIDER_NAME,
    google_oauth_settings_from_provider,
)
from wybra.providers.settings import ProviderSettings


@dataclass(frozen=True, slots=True)
class ProviderAuthDescriptor:
    name: str
    label: str
    login_route_name: str
    link_route_name: str
    security_unlink_route_name: str
    settings_factory: Callable[[ProviderSettings], object]


_PROVIDER_AUTH_DESCRIPTORS = (
    ProviderAuthDescriptor(
        name=GOOGLE_PROVIDER_NAME,
        label="Google",
        login_route_name="auth:google-login",
        link_route_name="auth:google-link",
        security_unlink_route_name="auth:security-google-unlink",
        settings_factory=google_oauth_settings_from_provider,
    ),
    ProviderAuthDescriptor(
        name=GITHUB_PROVIDER_NAME,
        label="GitHub",
        login_route_name="auth:github-login",
        link_route_name="auth:github-link",
        security_unlink_route_name="auth:security-github-unlink",
        settings_factory=github_oauth_settings_from_provider,
    ),
    ProviderAuthDescriptor(
        name=APPLE_PROVIDER_NAME,
        label="Apple",
        login_route_name="auth:apple-login",
        link_route_name="auth:apple-link",
        security_unlink_route_name="auth:security-apple-unlink",
        settings_factory=apple_oauth_settings_from_provider,
    ),
)


def provider_auth_descriptors() -> tuple[ProviderAuthDescriptor, ...]:
    return _PROVIDER_AUTH_DESCRIPTORS


def provider_auth_descriptor(provider_name: str) -> ProviderAuthDescriptor:
    for descriptor in _PROVIDER_AUTH_DESCRIPTORS:
        if descriptor.name == provider_name:
            return descriptor
    raise ConfigurationError(f"Unknown provider descriptor: {provider_name}.")


def provider_label(provider_name: str) -> str:
    return provider_auth_descriptor(provider_name).label
