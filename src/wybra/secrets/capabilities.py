from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from wybra.config import ConfigService
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

    @classmethod
    def from_settings(
        cls,
        settings: SecretsSettings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> DefaultSecretsCapability:
        return cls.from_drivers(
            (
                EnvironmentSecretSourceDriver(
                    environ if environ is not None else os.environ
                ),
                AwsSecretsManagerSourceDriver(settings.kms),
                KeychainSecretSourceDriver(settings.keychain),
                VaultSecretSourceDriver(settings.vault),
            )
        )

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
        DefaultSecretsCapability.from_settings(
            settings,
            environ=_resolved_environ(site),
        ),
    )


def _resolved_environ(site: Site) -> Mapping[str, str]:
    return site.config.environ if site.config.environ is not None else os.environ


def secrets_capability_from_config(config: ConfigService) -> DefaultSecretsCapability:
    return DefaultSecretsCapability.from_settings(
        SecretsSettings.load_settings(config),
        environ=config.environ,
    )


__all__ = (
    "DefaultSecretsCapability",
    "SecretsCapability",
    "secrets_capability_from_config",
    "setup_site",
)
