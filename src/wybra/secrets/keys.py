from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from wybra.auth.settings import (
    PROVIDER_CLIENT_SECRET_KEY_FIELD,
    PROVIDER_ENABLED_FIELD,
    PROVIDER_SECRETS_FIELD,
    AuthProviderSecretReference,
)
from wybra.core.exceptions import ConfigurationError
from wybra.secrets.config import SecretsSettings
from wybra.services.crypto import (
    ENV_WYBRA_SECRET_KEY_CURRENT,
    ENV_WYBRA_SECRET_KEYS_PREVIOUS,
)
from wybra.services.secrets import KEYCHAIN_SOURCE, SecretSource

SECRET_KEY_OWNER_CRYPTO: Final = "crypto"
SECRET_KEY_OWNER_AUTH: Final = "auth"


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
    keys.extend(_configured_auth_provider_keys(raw_config))
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


def _configured_auth_provider_keys(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> Iterable[KnownSecretKey]:
    providers_config = raw_config.get("auth.providers")
    if providers_config is None:
        return ()
    if not isinstance(providers_config, Mapping):
        raise ConfigurationError(
            "Auth providers config must be an [auth.providers] table."
        )

    keys: list[KnownSecretKey] = []
    for provider_name, provider_config in providers_config.items():
        if not isinstance(provider_config, Mapping):
            raise ConfigurationError(
                f"Auth provider {provider_name!r} config must be a table."
            )
        reference = AuthProviderSecretReference(
            name=str(provider_name),
            enabled=provider_config.get(PROVIDER_ENABLED_FIELD, True),
            secrets=provider_config.get(PROVIDER_SECRETS_FIELD),
            client_secret_key=provider_config.get(PROVIDER_CLIENT_SECRET_KEY_FIELD),
        )
        required_reference = reference.required_client_secret_reference()
        if required_reference is None:
            continue
        source, key = required_reference
        if source != KEYCHAIN_SOURCE:
            continue
        keys.append(
            KnownSecretKey(
                key=key,
                owner=SECRET_KEY_OWNER_AUTH,
                description=f"Auth provider {reference.name} client secret.",
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
    "SECRET_KEY_OWNER_AUTH",
    "SECRET_KEY_OWNER_CRYPTO",
    "known_keychain_secret_keys",
)
