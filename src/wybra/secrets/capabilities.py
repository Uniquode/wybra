from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from wybra.secrets.config import SecretsSettings
from wybra.secrets.sources import (
    AwsSecretsManagerSourceDriver,
    EnvironmentSecretSourceDriver,
    KeychainSecretSourceDriver,
    SecretSourceDriver,
    VaultSecretSourceDriver,
)
from wybra.services.secrets import (
    SecretsCapability,
    SecretSource,
    SecretValue,
    UnknownSecretSourceError,
    normalise_secret_source,
    secret_key_value,
)
from wybra.site import Site


@dataclass(frozen=True, slots=True)
class DefaultSecretsCapability:
    _drivers: Mapping[SecretSource, SecretSourceDriver] = field(repr=False)

    @classmethod
    def from_drivers(
        cls,
        drivers: Iterable[SecretSourceDriver],
    ) -> DefaultSecretsCapability:
        registry: dict[SecretSource, SecretSourceDriver] = {}
        for driver in drivers:
            source = normalise_secret_source(driver.source)
            registry[source] = driver
        return cls(_drivers=registry)

    @property
    def sources(self) -> tuple[SecretSource, ...]:
        return tuple(self._drivers)

    def resolve(self, source: SecretSource | str, key: str) -> SecretValue:
        driver = self._driver(source)
        return driver.resolve(secret_key_value(key))

    def exists(self, source: SecretSource | str, key: str) -> bool:
        driver = self._driver(source)
        return driver.exists(secret_key_value(key))

    def _driver(self, source: SecretSource | str) -> SecretSourceDriver:
        source_name = normalise_secret_source(source)
        try:
            return self._drivers[source_name]
        except KeyError as exc:
            raise UnknownSecretSourceError(source=source_name) from exc


async def setup_site(site: Site) -> None:
    settings = SecretsSettings.load_settings(site.config)
    site.provide_capability(
        SecretsCapability,
        DefaultSecretsCapability.from_drivers(
            (
                EnvironmentSecretSourceDriver(_resolved_environ(site)),
                AwsSecretsManagerSourceDriver(settings.kms),
                KeychainSecretSourceDriver(settings.keychain),
                VaultSecretSourceDriver(settings.vault),
            )
        ),
    )


def _resolved_environ(site: Site) -> Mapping[str, str]:
    return site.config.environ if site.config.environ is not None else os.environ


__all__ = (
    "DefaultSecretsCapability",
    "SecretsCapability",
    "setup_site",
)
