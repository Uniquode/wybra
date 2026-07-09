from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from wybra.config import BaseSettings, ConfigDef, ConfigField, ConfigGroup
from wybra.services.crypto import (
    ENV_WYBRA_SECRET_KEY,
    SECRET_KEY_CURRENT,
    SECRET_KEY_PREVIOUS,
)
from wybra.services.secrets import (
    ENVIRONMENT_SOURCE,
    KEYCHAIN_SOURCE,
    SecretSource,
    normalise_secret_source,
)

SECRETS_ENVIRONMENT_SECTION: Final = "secrets.environment"
SECRETS_CRYPTO_SECTION: Final = "secrets.crypto"
SECRETS_KMS_SECTION: Final = "secrets.kms"
SECRETS_KEYCHAIN_SECTION: Final = "secrets.keychain"
SECRETS_VAULT_SECTION: Final = "secrets.vault"

DEFAULT_KEYCHAIN_APPNAME: Final = "wybra"
DEFAULT_VAULT_MOUNT_POINT: Final = "secret"


def _optional_non_blank_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("must be a non-blank string when configured.")


def _optional_secret_source(value: object) -> SecretSource | None:
    if value is None:
        return None
    return normalise_secret_source(value, name="secrets crypto source")


def _non_blank_string(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("must be a non-blank string.")


module_config: Final = ConfigDef(
    {
        SECRETS_ENVIRONMENT_SECTION: ConfigGroup(),
        SECRETS_CRYPTO_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="source", transform=_optional_secret_source),
                ConfigField(
                    name="current_key",
                    default=SECRET_KEY_CURRENT,
                    transform=_non_blank_string,
                ),
                ConfigField(
                    name="previous_keys",
                    default=SECRET_KEY_PREVIOUS,
                    transform=_optional_non_blank_string,
                ),
            ),
        ),
        SECRETS_KMS_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="region_name", transform=_optional_non_blank_string),
                ConfigField(name="base_path", transform=_optional_non_blank_string),
            ),
        ),
        SECRETS_KEYCHAIN_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="appname",
                    default=DEFAULT_KEYCHAIN_APPNAME,
                    transform=_non_blank_string,
                ),
                ConfigField(name="username", transform=_optional_non_blank_string),
            ),
        ),
        SECRETS_VAULT_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="url", transform=_optional_non_blank_string),
                ConfigField(
                    name="mount_point",
                    default=DEFAULT_VAULT_MOUNT_POINT,
                    transform=_non_blank_string,
                ),
                ConfigField(name="secrets_path", transform=_optional_non_blank_string),
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class KmsSecretSourceSettings:
    region_name: str | None = None
    base_path: str | None = None


@dataclass(frozen=True, slots=True)
class CryptoSecretSourceSettings:
    source: SecretSource | None = None
    current_key: str = SECRET_KEY_CURRENT
    previous_keys: str | None = SECRET_KEY_PREVIOUS

    def __post_init__(self) -> None:
        source = _optional_secret_source(self.source)
        current_key = _non_blank_string(self.current_key)
        previous_keys = _optional_non_blank_string(self.previous_keys)
        if source == ENVIRONMENT_SOURCE:
            if current_key == SECRET_KEY_CURRENT:
                current_key = ENV_WYBRA_SECRET_KEY
            if previous_keys == SECRET_KEY_PREVIOUS:
                previous_keys = None
        elif source == KEYCHAIN_SOURCE:
            if previous_keys is None:
                previous_keys = SECRET_KEY_PREVIOUS
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "current_key", current_key)
        object.__setattr__(self, "previous_keys", previous_keys)


@dataclass(frozen=True, slots=True)
class KeychainSecretSourceSettings:
    appname: str = DEFAULT_KEYCHAIN_APPNAME
    username: str | None = None


@dataclass(frozen=True, slots=True)
class VaultSecretSourceSettings:
    url: str | None = None
    mount_point: str = DEFAULT_VAULT_MOUNT_POINT
    secrets_path: str | None = None


@dataclass(frozen=True, slots=True)
class SecretsSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config

    crypto: CryptoSecretSourceSettings = CryptoSecretSourceSettings()
    kms: KmsSecretSourceSettings = KmsSecretSourceSettings()
    keychain: KeychainSecretSourceSettings = KeychainSecretSourceSettings()
    vault: VaultSecretSourceSettings = VaultSecretSourceSettings()

    @classmethod
    def load_settings(cls, config) -> SecretsSettings:  # type: ignore[override]
        crypto_values = cls.section_values(config, SECRETS_CRYPTO_SECTION)
        kms_values = cls.section_values(config, SECRETS_KMS_SECTION)
        keychain_values = cls.section_values(config, SECRETS_KEYCHAIN_SECTION)
        vault_values = cls.section_values(config, SECRETS_VAULT_SECTION)
        return cls(
            crypto=CryptoSecretSourceSettings(**crypto_values),
            kms=KmsSecretSourceSettings(**kms_values),
            keychain=KeychainSecretSourceSettings(**keychain_values),
            vault=VaultSecretSourceSettings(**vault_values),
        )


def validate_secret_source(value: object) -> str:
    return normalise_secret_source(value)


__all__ = (
    "SECRET_KEY_CURRENT",
    "SECRET_KEY_PREVIOUS",
    "CryptoSecretSourceSettings",
    "DEFAULT_KEYCHAIN_APPNAME",
    "DEFAULT_VAULT_MOUNT_POINT",
    "KeychainSecretSourceSettings",
    "KmsSecretSourceSettings",
    "SECRETS_CRYPTO_SECTION",
    "SECRETS_ENVIRONMENT_SECTION",
    "SECRETS_KEYCHAIN_SECTION",
    "SECRETS_KMS_SECTION",
    "SECRETS_VAULT_SECTION",
    "SecretsSettings",
    "VaultSecretSourceSettings",
    "module_config",
    "validate_secret_source",
)
