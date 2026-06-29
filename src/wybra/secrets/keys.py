from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from wybra.core.exceptions import ConfigurationError
from wybra.providers.secrets import provider_keychain_secret_references
from wybra.providers.settings import (
    PROVIDERS_CONFIG_SECTION,
    provider_settings_from_config,
)
from wybra.secrets.config import SecretsSettings
from wybra.services.crypto import (
    ENV_WYBRA_SECRET_KEY_CURRENT,
    ENV_WYBRA_SECRET_KEYS_PREVIOUS,
)
from wybra.services.secrets import KEYCHAIN_SOURCE, SecretSource

SECRET_KEY_OWNER_CRYPTO: Final = "crypto"
SECRET_KEY_OWNER_PROVIDERS: Final = "providers"


@dataclass(frozen=True, slots=True)
class KnownSecretKey:
    key: str
    owner: str
    description: str
    source: SecretSource = KEYCHAIN_SOURCE
    required: bool = False


BUILTIN_CRYPTO_SECRET_KEYS: Final[tuple[KnownSecretKey, ...]] = (
    KnownSecretKey(
        key=ENV_WYBRA_SECRET_KEY_CURRENT,
        owner=SECRET_KEY_OWNER_CRYPTO,
        description="Current system secret key.",
        required=True,
    ),
    KnownSecretKey(
        key=ENV_WYBRA_SECRET_KEYS_PREVIOUS,
        owner=SECRET_KEY_OWNER_CRYPTO,
        description="Previous system secret keys used during key rotation.",
    ),
)


def known_keychain_secret_keys(
    *,
    raw_config: Mapping[str, Mapping[str, Any]] | None = None,
    secrets_settings: SecretsSettings | None = None,
) -> tuple[KnownSecretKey, ...]:
    """Return Wybra-known keychain references without enumerating the keychain."""

    if raw_config is None or secrets_settings is None:
        return _deduplicate_keys(BUILTIN_CRYPTO_SECRET_KEYS)

    keys: list[KnownSecretKey] = []
    keys.extend(_configured_crypto_keys(secrets_settings))
    keys.extend(_configured_provider_keys(raw_config))
    return _deduplicate_keys(keys)


def _configured_crypto_keys(
    settings: SecretsSettings,
) -> Iterable[KnownSecretKey]:
    if settings.crypto.source != KEYCHAIN_SOURCE:
        return ()

    keys = [
        KnownSecretKey(
            key=settings.crypto.current_key,
            owner=SECRET_KEY_OWNER_CRYPTO,
            description="Configured current system secret key.",
            required=True,
        )
    ]
    if settings.crypto.previous_keys is not None:
        keys.append(
            KnownSecretKey(
                key=settings.crypto.previous_keys,
                owner=SECRET_KEY_OWNER_CRYPTO,
                description="Configured previous system secret keys for rotation.",
            )
        )
    return tuple(keys)


def _configured_provider_keys(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> Iterable[KnownSecretKey]:
    providers_config = raw_config.get(PROVIDERS_CONFIG_SECTION)
    if providers_config is None:
        return ()
    if not isinstance(providers_config, Mapping):
        raise ConfigurationError(
            f"Providers config must be a [{PROVIDERS_CONFIG_SECTION}] table."
        )

    keys: list[KnownSecretKey] = []
    providers = provider_settings_from_config(providers_config)
    for provider_name, key in provider_keychain_secret_references(providers):
        keys.append(
            KnownSecretKey(
                key=key,
                owner=SECRET_KEY_OWNER_PROVIDERS,
                description=f"Provider {provider_name} client secret.",
                required=True,
            )
        )
    return tuple(keys)


def _deduplicate_keys(keys: Iterable[KnownSecretKey]) -> tuple[KnownSecretKey, ...]:
    deduplicated: dict[str, KnownSecretKey] = {}
    for key in keys:
        existing = deduplicated.get(key.key)
        if existing is None:
            deduplicated[key.key] = key
            continue
        deduplicated[key.key] = KnownSecretKey(
            key=existing.key,
            owner=existing.owner,
            description=existing.description,
            source=existing.source,
            required=existing.required or key.required,
        )
    return tuple(deduplicated.values())


__all__ = (
    "BUILTIN_CRYPTO_SECRET_KEYS",
    "KnownSecretKey",
    "SECRET_KEY_OWNER_CRYPTO",
    "SECRET_KEY_OWNER_PROVIDERS",
    "known_keychain_secret_keys",
)
