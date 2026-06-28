from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from wybra.config import to_bool
from wybra.core.exceptions import ConfigurationError
from wybra.services.secrets import (
    SecretSource,
    normalise_secret_source,
    secret_key_value,
)

AUTH_PROVIDERS_CONFIG_SECTION: Final = "auth.providers"
PROVIDERS_SECTION_FIELD = "providers"
PROVIDER_ENABLED_FIELD = "enabled"
PROVIDER_SECRETS_FIELD = "secrets"
PROVIDER_CLIENT_SECRET_KEY_FIELD = "client_secret_key"
PROVIDER_CLIENT_ID_FIELD = "client_id"
PROVIDER_SECRET_OPTION_FIELDS: Final = frozenset(
    {
        PROVIDER_ENABLED_FIELD,
        PROVIDER_SECRETS_FIELD,
        PROVIDER_CLIENT_SECRET_KEY_FIELD,
        PROVIDER_CLIENT_ID_FIELD,
    }
)


@dataclass(frozen=True, slots=True)
class AuthProviderSecretReference:
    name: str
    enabled: bool = True
    secrets: str | None = None
    client_secret_key: str | None = None
    client_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", provider_name_value(self.name))
        object.__setattr__(self, "enabled", _provider_enabled_value(self.enabled))
        if self.secrets is not None:
            object.__setattr__(
                self,
                "secrets",
                _optional_provider_string(
                    self.secrets,
                    field_name=PROVIDER_SECRETS_FIELD,
                ),
            )
        if self.client_secret_key is not None:
            object.__setattr__(
                self,
                "client_secret_key",
                _optional_provider_string(
                    self.client_secret_key,
                    field_name=PROVIDER_CLIENT_SECRET_KEY_FIELD,
                ),
            )
        if self.client_id is not None:
            object.__setattr__(
                self,
                "client_id",
                _optional_provider_string(
                    self.client_id,
                    field_name=PROVIDER_CLIENT_ID_FIELD,
                ),
            )

    def required_client_secret_reference(self) -> tuple[SecretSource, str] | None:
        if not self.enabled:
            return None
        if self.secrets is None and self.client_secret_key is None:
            return None
        if self.secrets is None or self.client_secret_key is None:
            raise ConfigurationError(
                f"Auth provider {self.name!r} must configure both "
                f"{PROVIDER_SECRETS_FIELD!r} and "
                f"{PROVIDER_CLIENT_SECRET_KEY_FIELD!r}, or neither."
            )
        source = normalise_secret_source(
            self.secrets,
            name=f"auth provider {self.name!r} secrets",
        )
        key = secret_key_value(
            self.client_secret_key,
            name=f"auth provider {self.name!r} client secret key",
        )
        return source, key


def provider_secret_references_from_config(
    auth_config: Mapping[str, Any],
) -> tuple[AuthProviderSecretReference, ...]:
    return tuple(
        AuthProviderSecretReference(
            name=provider_name,
            enabled=provider_config.get(PROVIDER_ENABLED_FIELD, True),
            secrets=cast(str | None, provider_config.get(PROVIDER_SECRETS_FIELD)),
            client_secret_key=cast(
                str | None,
                provider_config.get(PROVIDER_CLIENT_SECRET_KEY_FIELD),
            ),
            client_id=cast(str | None, provider_config.get(PROVIDER_CLIENT_ID_FIELD)),
        )
        for provider_name, provider_config in provider_configs(auth_config)
    )


def provider_configs(
    auth_config: Mapping[str, Any],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    providers_config = auth_config.get(PROVIDERS_SECTION_FIELD)
    if providers_config is None:
        return ()
    if not isinstance(providers_config, Mapping):
        raise ConfigurationError(
            "Auth providers config must be an [auth.providers] table."
        )

    configs: list[tuple[str, Mapping[str, Any]]] = []
    for provider_name, provider_config in providers_config.items():
        name = provider_name_value(provider_name)
        if not isinstance(provider_config, Mapping):
            raise ConfigurationError(f"Auth provider {name!r} config must be a table.")
        configs.append((name, provider_config))
    return tuple(configs)


def reject_unknown_provider_options(auth_config: Mapping[str, Any]) -> None:
    for provider_name, provider_config in provider_configs(auth_config):
        unknown_fields = sorted(set(provider_config) - PROVIDER_SECRET_OPTION_FIELDS)
        if unknown_fields:
            unknown_list = ", ".join(unknown_fields)
            allowed_fields = ", ".join(sorted(PROVIDER_SECRET_OPTION_FIELDS))
            raise ConfigurationError(
                f"Unknown option(s) in [auth.providers.{provider_name}] "
                f"configuration: {unknown_list}. Allowed options are: "
                f"{allowed_fields}."
            )


def provider_secret_reference(
    references: Iterable[AuthProviderSecretReference],
    provider_name: str,
) -> AuthProviderSecretReference:
    name = provider_name_value(provider_name)
    for provider in references:
        if provider.name == name:
            return provider
    raise ConfigurationError(f"Unknown auth provider configuration: {name}.")


def provider_name_value(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigurationError("Auth provider name must be a non-blank string.")


def _provider_enabled_value(value: object) -> bool:
    try:
        return to_bool(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "Auth provider enabled value must be boolean."
        ) from exc


def _optional_provider_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
        raise ConfigurationError(
            f"Auth provider {field_name} must not be blank or whitespace-only."
        )
    raise ConfigurationError(f"Auth provider {field_name} must be a string.")


__all__ = (
    "AUTH_PROVIDERS_CONFIG_SECTION",
    "PROVIDERS_SECTION_FIELD",
    "PROVIDER_CLIENT_ID_FIELD",
    "PROVIDER_CLIENT_SECRET_KEY_FIELD",
    "PROVIDER_ENABLED_FIELD",
    "PROVIDER_SECRET_OPTION_FIELDS",
    "PROVIDER_SECRETS_FIELD",
    "AuthProviderSecretReference",
    "provider_configs",
    "provider_name_value",
    "provider_secret_reference",
    "provider_secret_references_from_config",
    "reject_unknown_provider_options",
)
