from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wybra.auth.capabilities import AuthCapability
from wybra.providers.secrets import validate_provider_secret_settings
from wybra.providers.settings import ProvidersSettings
from wybra.providers.validation import validate_provider_configuration
from wybra.services.secrets import SecretsCapability
from wybra.site import Site


@runtime_checkable
class ProvidersCapability(Protocol):
    @property
    def settings(self) -> ProvidersSettings: ...


@dataclass(frozen=True, slots=True)
class SiteProvidersCapability:
    settings: ProvidersSettings


async def setup_site(site: Site) -> None:
    settings = ProvidersSettings.load_settings(site.config)
    site.provide_capability(ProvidersCapability, SiteProvidersCapability(settings))


async def post_setup_site(site: Site) -> None:
    site.require_capability(AuthCapability)
    settings = site.require_capability(ProvidersCapability).settings
    validate_provider_configuration(settings)
    secrets = None
    if settings.enabled_providers:
        secrets = site.require_capability(SecretsCapability)
    validate_provider_secret_settings(
        settings,
        secrets,
    )


__all__ = (
    "ProvidersCapability",
    "SiteProvidersCapability",
    "post_setup_site",
    "setup_site",
)
