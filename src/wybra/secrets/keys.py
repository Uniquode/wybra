from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from wybra.config import ConfigService, CredentialReference, MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.forms.config import (
    CSRF_TOKEN_SECRET_KEY_CURRENT,
    CSRF_TOKEN_SECRET_KEY_PREVIOUS,
)
from wybra.forms.settings import FormsSettings
from wybra.providers.settings import (
    PROVIDERS_CONFIG_SECTION,
    provider_client_secret_key,
    provider_private_key_secret_key,
    provider_settings_from_config,
)
from wybra.secrets.config import SecretsSettings
from wybra.services.crypto import SECRET_KEY_CURRENT, SECRET_KEY_PREVIOUS
from wybra.services.secrets import KEYCHAIN_SOURCE

SECRET_KEY_TYPE_SECRET: Final = "secret"
SECRET_KEY_TYPE_SECRET_PREVIOUS: Final = "secret-prev"
SECRET_KEY_TYPE_CSRF: Final = "csrf"
SECRET_KEY_TYPE_CSRF_PREVIOUS: Final = "csrf-prev"
SECRET_KEY_TYPE_GOOGLE: Final = "google"
SECRET_KEY_TYPE_GITHUB: Final = "github"
SECRET_KEY_TYPE_APPLE: Final = "apple"

SECRET_KEY_OWNER_CRYPTO: Final = "crypto"
SECRET_KEY_OWNER_FORMS: Final = "forms"
SECRET_KEY_OWNER_PROVIDERS: Final = "providers"


BUILTIN_CRYPTO_SECRET_KEYS: Final[tuple[CredentialReference, ...]] = (
    CredentialReference(
        name=SECRET_KEY_TYPE_SECRET,
        key=SECRET_KEY_CURRENT,
        owner=SECRET_KEY_OWNER_CRYPTO,
        description="Current system secret key.",
        source=KEYCHAIN_SOURCE,
        required=True,
        rotation_role="current",
    ),
    CredentialReference(
        name=SECRET_KEY_TYPE_SECRET_PREVIOUS,
        key=SECRET_KEY_PREVIOUS,
        owner=SECRET_KEY_OWNER_CRYPTO,
        description="Previous system secret keys used during key rotation.",
        source=KEYCHAIN_SOURCE,
        rotation_role="previous",
    ),
)
BUILTIN_FORMS_SECRET_KEYS: Final[tuple[CredentialReference, ...]] = (
    CredentialReference(
        name=SECRET_KEY_TYPE_CSRF,
        key=CSRF_TOKEN_SECRET_KEY_CURRENT,
        owner=SECRET_KEY_OWNER_FORMS,
        description="Current forms CSRF token secret.",
        source=KEYCHAIN_SOURCE,
        required=True,
        rotation_role="current",
    ),
    CredentialReference(
        name=SECRET_KEY_TYPE_CSRF_PREVIOUS,
        key=CSRF_TOKEN_SECRET_KEY_PREVIOUS,
        owner=SECRET_KEY_OWNER_FORMS,
        description="Previous forms CSRF token secrets.",
        source=KEYCHAIN_SOURCE,
        rotation_role="previous",
    ),
)
BUILTIN_PROVIDER_SECRET_KEYS: Final[tuple[CredentialReference, ...]] = (
    CredentialReference(
        name=SECRET_KEY_TYPE_GOOGLE,
        key=provider_client_secret_key(SECRET_KEY_TYPE_GOOGLE),
        owner=SECRET_KEY_OWNER_PROVIDERS,
        description="Provider google client secret.",
        source=KEYCHAIN_SOURCE,
        required=True,
    ),
    CredentialReference(
        name=SECRET_KEY_TYPE_GITHUB,
        key=provider_client_secret_key(SECRET_KEY_TYPE_GITHUB),
        owner=SECRET_KEY_OWNER_PROVIDERS,
        description="Provider github client secret.",
        source=KEYCHAIN_SOURCE,
        required=True,
    ),
    CredentialReference(
        name=SECRET_KEY_TYPE_APPLE,
        key=provider_private_key_secret_key(SECRET_KEY_TYPE_APPLE),
        owner=SECRET_KEY_OWNER_PROVIDERS,
        description="Provider apple private key.",
        source=KEYCHAIN_SOURCE,
        required=True,
    ),
)
BUILTIN_SECRET_KEYS: Final[tuple[CredentialReference, ...]] = (
    BUILTIN_CRYPTO_SECRET_KEYS
    + BUILTIN_FORMS_SECRET_KEYS
    + BUILTIN_PROVIDER_SECRET_KEYS
)


def known_keychain_secret_keys(
    *,
    raw_config: Mapping[str, Mapping[str, Any]] | None = None,
    secrets_settings: SecretsSettings | None = None,
    development: bool = False,
) -> tuple[CredentialReference, ...]:
    """Return Wybra-known keychain references without enumerating the keychain."""

    if development:
        return _deduplicate_keys(development_keychain_secret_keys())
    if raw_config is None or secrets_settings is None:
        return _deduplicate_keys(BUILTIN_SECRET_KEYS)

    keys: list[CredentialReference] = []
    keys.extend(_configured_crypto_keys(secrets_settings))
    keys.extend(_configured_forms_keys(raw_config))
    keys.extend(_configured_provider_keys(raw_config))
    return _deduplicate_keys(keys)


def development_keychain_secret_keys() -> tuple[CredentialReference, ...]:
    """Return built-in development keychain references."""
    return tuple(
        CredentialReference(
            name=known_key.name,
            key=builtin_keychain_secret_key(known_key.name, development=True),
            owner=known_key.owner,
            description=known_key.description,
            source=known_key.source,
            required=known_key.required,
            rotation_role=known_key.rotation_role,
        )
        for known_key in BUILTIN_SECRET_KEYS
    )


def _configured_crypto_keys(
    settings: SecretsSettings,
) -> Iterable[CredentialReference]:
    return tuple(
        reference
        for reference in settings.credential_references()
        if reference.source == KEYCHAIN_SOURCE
    )


def _configured_provider_keys(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> Iterable[CredentialReference]:
    providers_config = raw_config.get(PROVIDERS_CONFIG_SECTION)
    if providers_config is None:
        return ()
    if not isinstance(providers_config, Mapping):
        raise ConfigurationError(
            f"Providers config must be a [{PROVIDERS_CONFIG_SECTION}] table."
        )

    providers = provider_settings_from_config(providers_config)
    return tuple(
        reference
        for provider in providers
        for reference in provider.credential_references()
        if reference.source == KEYCHAIN_SOURCE
    )


def _configured_forms_keys(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> Iterable[CredentialReference]:
    config = ConfigService(
        [MappingConfigSource(raw_config)],
        config_defs=(FormsSettings.module_config,),
        discover_module_config=False,
    )
    settings = FormsSettings.load_settings(config)
    return tuple(
        reference
        for reference in settings.credential_references()
        if reference.source == KEYCHAIN_SOURCE
    )


def builtin_keychain_secret_key(name: str, *, development: bool = False) -> str:
    """Return the built-in keychain key for a named Wybra secret type."""
    key_type = normalise_secret_key_type(name)
    if key_type == SECRET_KEY_TYPE_SECRET:
        return _development_key(SECRET_KEY_CURRENT, development=development)
    if key_type == SECRET_KEY_TYPE_SECRET_PREVIOUS:
        return _development_key(SECRET_KEY_PREVIOUS, development=development)
    if key_type == SECRET_KEY_TYPE_CSRF:
        return _development_key(
            CSRF_TOKEN_SECRET_KEY_CURRENT,
            development=development,
        )
    if key_type == SECRET_KEY_TYPE_CSRF_PREVIOUS:
        return _development_key(
            CSRF_TOKEN_SECRET_KEY_PREVIOUS,
            development=development,
        )
    if key_type == SECRET_KEY_TYPE_GOOGLE:
        return provider_client_secret_key(
            SECRET_KEY_TYPE_GOOGLE, development=development
        )
    if key_type == SECRET_KEY_TYPE_GITHUB:
        return provider_client_secret_key(
            SECRET_KEY_TYPE_GITHUB, development=development
        )
    if key_type == SECRET_KEY_TYPE_APPLE:
        return provider_private_key_secret_key(
            SECRET_KEY_TYPE_APPLE,
            development=development,
        )
    raise ConfigurationError(f"Unknown secret type: {name}.")


def normalise_secret_key_type(name: str) -> str:
    key_type = name.strip().lower()
    if key_type:
        return key_type
    raise ConfigurationError("Secret type must not be blank.")


def _development_key(key: str, *, development: bool) -> str:
    if not development:
        return key
    *parent, leaf = key.split("/")
    return "/".join((*parent, "dev", leaf))


def _deduplicate_keys(
    keys: Iterable[CredentialReference],
) -> tuple[CredentialReference, ...]:
    deduplicated: dict[str, CredentialReference] = {}
    for key in keys:
        existing = deduplicated.get(key.name)
        if existing is None:
            deduplicated[key.name] = key
            continue
        deduplicated[key.name] = CredentialReference(
            name=existing.name,
            key=existing.key,
            owner=existing.owner,
            description=existing.description,
            source=existing.source,
            required=existing.required or key.required,
            rotation_role=existing.rotation_role,
        )
    return tuple(deduplicated.values())


__all__ = (
    "BUILTIN_SECRET_KEYS",
    "BUILTIN_CRYPTO_SECRET_KEYS",
    "BUILTIN_FORMS_SECRET_KEYS",
    "BUILTIN_PROVIDER_SECRET_KEYS",
    "SECRET_KEY_TYPE_APPLE",
    "SECRET_KEY_TYPE_CSRF",
    "SECRET_KEY_TYPE_CSRF_PREVIOUS",
    "SECRET_KEY_TYPE_GITHUB",
    "SECRET_KEY_TYPE_GOOGLE",
    "SECRET_KEY_TYPE_SECRET",
    "SECRET_KEY_TYPE_SECRET_PREVIOUS",
    "SECRET_KEY_OWNER_CRYPTO",
    "SECRET_KEY_OWNER_FORMS",
    "SECRET_KEY_OWNER_PROVIDERS",
    "builtin_keychain_secret_key",
    "development_keychain_secret_keys",
    "known_keychain_secret_keys",
    "normalise_secret_key_type",
)
