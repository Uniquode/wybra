from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wybra.auth.capabilities import AuthCapability
from wybra.providers.secrets import provider_settings_with_available_secrets
from wybra.providers.settings import ProvidersSettings
from wybra.providers.validation import validate_provider_configuration
from wybra.services.secrets import SecretsCapability
from wybra.site import Site

logger = logging.getLogger(__name__)


@runtime_checkable
class ProvidersCapability(Protocol):
    @property
    def settings(self) -> ProvidersSettings: ...


@dataclass(slots=True)
class SiteProvidersCapability:
    settings: ProvidersSettings


async def setup_site(site: Site) -> None:
    settings = ProvidersSettings.load_settings(site.config)
    validate_provider_configuration(settings)
    site.provide_capability(ProvidersCapability, SiteProvidersCapability(settings))


async def post_setup_site(site: Site) -> None:
    site.require_capability(AuthCapability)
    capability = site.require_capability(ProvidersCapability)
    secrets = (
        site.optional_capability(SecretsCapability)
        if capability.settings.enabled_providers
        else None
    )
    settings, secret_issues = provider_settings_with_available_secrets(
        capability.settings,
        secrets,
    )
    for issue in secret_issues:
        logger.error(
            "Provider %r disabled: %s.",
            issue.provider_name,
            issue.message,
        )
    if isinstance(capability, SiteProvidersCapability):
        capability.settings = settings


__all__ = (
    "ProvidersCapability",
    "SiteProvidersCapability",
    "post_setup_site",
    "setup_site",
)
