"""Runtime capability for web-facing security policy."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, runtime_checkable

from wybra.security.cors import CorsPolicySet
from wybra.security.headers import SecurityHeaderOptions, register_security_headers
from wybra.security.settings import SecuritySettings
from wybra.site import Site


@runtime_checkable
class SecurityCapability(Protocol):
    header_options: SecurityHeaderOptions
    asset_cors: CorsPolicySet


@dataclass(frozen=True, slots=True)
class DefaultSecurityCapability:
    settings: SecuritySettings

    @property
    def header_options(self) -> SecurityHeaderOptions:
        return self.settings.header_options

    @property
    def asset_cors(self) -> CorsPolicySet:
        return self.settings.asset_cors


async def setup_site(site: Site) -> None:
    settings = SecuritySettings.load_settings(site.config)
    capability = DefaultSecurityCapability(settings=settings)
    site.provide_capability(SecurityCapability, capability)
    register_security_headers(site.app, options=capability.header_options)


async def post_setup_site(site: Site) -> None:
    """Reserved for future hard security dependency checks."""


def security_provider_configured(modules: tuple[str, ...]) -> bool:
    """Return whether configured modules include a security capability provider."""
    for module_name in modules:
        if module_name == "wybra.security":
            return True
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        if getattr(module, "provides_security_capability", False) is True:
            return True
    return False
