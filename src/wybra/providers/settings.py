from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, cast

from wybra.config import BaseSettings, ConfigDef, ConfigGroup, to_bool
from wybra.core.exceptions import ConfigurationError
from wybra.services.secrets import (
    SecretSource,
    normalise_secret_source,
    secret_key_value,
)

PROVIDERS_CONFIG_SECTION: Final = "auth.providers"
APPLE_PROVIDER_NAME: Final = "apple"
PROVIDER_ENABLED_FIELD: Final = "enabled"
PROVIDER_CLIENT_ID_FIELD: Final = "client_id"
PROVIDER_SECRETS_FIELD: Final = "secrets"
PROVIDER_CLIENT_SECRET_KEY_FIELD: Final = "client_secret_key"
PROVIDER_TEAM_ID_FIELD: Final = "team_id"
PROVIDER_KEY_ID_FIELD: Final = "key_id"
PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD: Final = "private_key_secret_key"
PROVIDER_ACCOUNT_CREATION_ENABLED_FIELD: Final = "account_creation_enabled"
PROVIDER_EMAIL_MATCH_LINKING_ENABLED_FIELD: Final = "email_match_linking_enabled"
PROVIDER_REQUIRED_CLAIMS_FIELD: Final = "required_claims"
PROVIDER_ALLOWED_EMAILS_FIELD: Final = "allowed_emails"
PROVIDER_ALLOWED_DOMAINS_FIELD: Final = "allowed_domains"
BASE_PROVIDER_OPTION_FIELDS: Final = frozenset(
    {
        PROVIDER_ACCOUNT_CREATION_ENABLED_FIELD,
        PROVIDER_ALLOWED_DOMAINS_FIELD,
        PROVIDER_ALLOWED_EMAILS_FIELD,
        PROVIDER_CLIENT_ID_FIELD,
        PROVIDER_CLIENT_SECRET_KEY_FIELD,
        PROVIDER_EMAIL_MATCH_LINKING_ENABLED_FIELD,
        PROVIDER_ENABLED_FIELD,
        PROVIDER_REQUIRED_CLAIMS_FIELD,
        PROVIDER_SECRETS_FIELD,
    }
)
APPLE_PROVIDER_OPTION_FIELDS: Final = frozenset(
    {
        PROVIDER_KEY_ID_FIELD,
        PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD,
        PROVIDER_TEAM_ID_FIELD,
    }
)
PROVIDER_OPTION_FIELDS: Final = (
    BASE_PROVIDER_OPTION_FIELDS | APPLE_PROVIDER_OPTION_FIELDS
)

module_config: Final = ConfigDef({PROVIDERS_CONFIG_SECTION: ConfigGroup()})


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    name: str
    enabled: bool = True
    client_id: str | None = None
    secrets: str | None = None
    client_secret_key: str | None = None
    team_id: str | None = None
    key_id: str | None = None
    private_key_secret_key: str | None = None
    account_creation_enabled: bool = False
    email_match_linking_enabled: bool = False
    required_claims: tuple[str, ...] = ()
    allowed_emails: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", provider_name_value(self.name))
        object.__setattr__(self, "enabled", _provider_enabled_value(self.enabled))
        object.__setattr__(
            self,
            "client_id",
            _optional_provider_string(
                self.client_id,
                field_name=PROVIDER_CLIENT_ID_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "secrets",
            _optional_provider_string(
                self.secrets,
                field_name=PROVIDER_SECRETS_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "client_secret_key",
            _optional_provider_string(
                self.client_secret_key,
                field_name=PROVIDER_CLIENT_SECRET_KEY_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "team_id",
            _optional_provider_string(
                self.team_id,
                field_name=PROVIDER_TEAM_ID_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "key_id",
            _optional_provider_string(
                self.key_id,
                field_name=PROVIDER_KEY_ID_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "private_key_secret_key",
            _optional_provider_string(
                self.private_key_secret_key,
                field_name=PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "account_creation_enabled",
            _provider_enabled_value(self.account_creation_enabled),
        )
        object.__setattr__(
            self,
            "email_match_linking_enabled",
            _provider_enabled_value(self.email_match_linking_enabled),
        )
        object.__setattr__(
            self,
            "required_claims",
            _normalise_string_tuple(
                self.required_claims,
                field_name=PROVIDER_REQUIRED_CLAIMS_FIELD,
            ),
        )
        object.__setattr__(
            self,
            "allowed_emails",
            tuple(
                item.lower()
                for item in _normalise_string_tuple(
                    self.allowed_emails,
                    field_name=PROVIDER_ALLOWED_EMAILS_FIELD,
                )
            ),
        )
        object.__setattr__(
            self,
            "allowed_domains",
            tuple(
                item.lower()
                for item in _normalise_string_tuple(
                    self.allowed_domains,
                    field_name=PROVIDER_ALLOWED_DOMAINS_FIELD,
                )
            ),
        )

    def required_client_secret_reference(self) -> tuple[SecretSource, str] | None:
        if not self.enabled:
            return None
        if self.secrets is None and self.client_secret_key is None:
            return None
        if self.secrets is None or self.client_secret_key is None:
            raise ConfigurationError(
                f"Provider {self.name!r} must configure both "
                f"{PROVIDER_SECRETS_FIELD!r} and "
                f"{PROVIDER_CLIENT_SECRET_KEY_FIELD!r}, or neither."
            )
        return (
            normalise_secret_source(
                self.secrets,
                name=f"provider {self.name!r} secrets",
            ),
            secret_key_value(
                self.client_secret_key,
                name=f"provider {self.name!r} client secret key",
            ),
        )

    def required_private_key_reference(self) -> tuple[SecretSource, str] | None:
        if not self.enabled:
            return None
        if self.secrets is None and self.private_key_secret_key is None:
            return None
        if self.secrets is None or self.private_key_secret_key is None:
            raise ConfigurationError(
                f"Provider {self.name!r} must configure both "
                f"{PROVIDER_SECRETS_FIELD!r} and "
                f"{PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD!r}, or neither."
            )
        return (
            normalise_secret_source(
                self.secrets,
                name=f"provider {self.name!r} secrets",
            ),
            secret_key_value(
                self.private_key_secret_key,
                name=f"provider {self.name!r} private key secret key",
            ),
        )

    def required_provider_secret_reference(
        self,
    ) -> tuple[SecretSource, str, str] | None:
        if self.name == APPLE_PROVIDER_NAME:
            reference = self.required_private_key_reference()
            if reference is None:
                return None
            source, key = reference
            return source, key, "private key"

        reference = self.required_client_secret_reference()
        if reference is None:
            return None
        source, key = reference
        return source, key, "client secret"


@dataclass(frozen=True, slots=True)
class ProvidersSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = PROVIDERS_CONFIG_SECTION

    providers: tuple[ProviderSettings, ...] = ()

    @classmethod
    def load_settings(cls, config) -> ProvidersSettings:  # type: ignore[override]
        return cls(
            providers=provider_settings_from_config(
                cls.section_values(config, PROVIDERS_CONFIG_SECTION)
            )
        )

    def provider(self, provider_name: str) -> ProviderSettings:
        name = provider_name_value(provider_name)
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ConfigurationError(f"Unknown provider configuration: {name}.")

    @property
    def enabled_providers(self) -> tuple[ProviderSettings, ...]:
        return tuple(provider for provider in self.providers if provider.enabled)


def provider_settings_from_config(
    providers_config: Mapping[str, Any],
) -> tuple[ProviderSettings, ...]:
    if not isinstance(providers_config, Mapping):
        raise ConfigurationError(
            f"Providers config must be a [{PROVIDERS_CONFIG_SECTION}] table."
        )

    providers: list[ProviderSettings] = []
    for provider_name, provider_config in providers_config.items():
        name = provider_name_value(provider_name)
        if not isinstance(provider_config, Mapping):
            raise ConfigurationError(f"Provider {name!r} config must be a table.")
        _reject_unknown_provider_options(name, provider_config)
        providers.append(
            ProviderSettings(
                name=name,
                enabled=provider_config.get(PROVIDER_ENABLED_FIELD, True),
                client_id=cast(
                    str | None,
                    provider_config.get(PROVIDER_CLIENT_ID_FIELD),
                ),
                secrets=cast(str | None, provider_config.get(PROVIDER_SECRETS_FIELD)),
                client_secret_key=cast(
                    str | None,
                    provider_config.get(PROVIDER_CLIENT_SECRET_KEY_FIELD),
                ),
                team_id=cast(
                    str | None,
                    provider_config.get(PROVIDER_TEAM_ID_FIELD),
                ),
                key_id=cast(
                    str | None,
                    provider_config.get(PROVIDER_KEY_ID_FIELD),
                ),
                private_key_secret_key=cast(
                    str | None,
                    provider_config.get(PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD),
                ),
                account_creation_enabled=provider_config.get(
                    PROVIDER_ACCOUNT_CREATION_ENABLED_FIELD,
                    False,
                ),
                email_match_linking_enabled=provider_config.get(
                    PROVIDER_EMAIL_MATCH_LINKING_ENABLED_FIELD,
                    False,
                ),
                required_claims=_tuple_config_value(
                    provider_config.get(PROVIDER_REQUIRED_CLAIMS_FIELD, ())
                ),
                allowed_emails=_tuple_config_value(
                    provider_config.get(PROVIDER_ALLOWED_EMAILS_FIELD, ())
                ),
                allowed_domains=_tuple_config_value(
                    provider_config.get(PROVIDER_ALLOWED_DOMAINS_FIELD, ())
                ),
            )
        )
    return tuple(providers)


def provider_name_value(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigurationError("Provider name must be a non-blank string.")


def _reject_unknown_provider_options(
    provider_name: str,
    provider_config: Mapping[str, Any],
) -> None:
    option_fields = _provider_option_fields(provider_name)
    unknown_fields = sorted(set(provider_config) - option_fields)
    if unknown_fields:
        unknown_list = ", ".join(unknown_fields)
        allowed_fields = ", ".join(sorted(option_fields))
        raise ConfigurationError(
            f"Unknown option(s) in "
            f"[{_provider_config_section(provider_name)}] configuration: "
            f"{unknown_list}. Allowed options are: {allowed_fields}."
        )


def _provider_option_fields(provider_name: str) -> frozenset[str]:
    if provider_name == APPLE_PROVIDER_NAME:
        return BASE_PROVIDER_OPTION_FIELDS | APPLE_PROVIDER_OPTION_FIELDS
    return BASE_PROVIDER_OPTION_FIELDS


def _provider_enabled_value(value: object) -> bool:
    try:
        return to_bool(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Provider enabled value must be boolean.") from exc


def _provider_config_section(provider_name: str) -> str:
    return f"{PROVIDERS_CONFIG_SECTION}.{provider_name}"


def _optional_provider_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
        raise ConfigurationError(
            f"Provider {field_name} must not be blank or whitespace-only."
        )
    raise ConfigurationError(f"Provider {field_name} must be a string.")


def _tuple_config_value(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigurationError(
                    "Provider list values must contain only strings."
                )
            items.append(item)
        return tuple(items)
    raise ConfigurationError("Provider list values must be strings or string lists.")


def _normalise_string_tuple(
    value: Iterable[object],
    *,
    field_name: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
            continue
        raise ConfigurationError(
            f"Provider {field_name} must contain only non-blank strings."
        )
    return tuple(values)


__all__ = (
    "APPLE_PROVIDER_NAME",
    "APPLE_PROVIDER_OPTION_FIELDS",
    "BASE_PROVIDER_OPTION_FIELDS",
    "PROVIDERS_CONFIG_SECTION",
    "PROVIDER_ACCOUNT_CREATION_ENABLED_FIELD",
    "PROVIDER_ALLOWED_DOMAINS_FIELD",
    "PROVIDER_ALLOWED_EMAILS_FIELD",
    "PROVIDER_CLIENT_ID_FIELD",
    "PROVIDER_CLIENT_SECRET_KEY_FIELD",
    "PROVIDER_EMAIL_MATCH_LINKING_ENABLED_FIELD",
    "PROVIDER_ENABLED_FIELD",
    "PROVIDER_KEY_ID_FIELD",
    "PROVIDER_OPTION_FIELDS",
    "PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD",
    "PROVIDER_REQUIRED_CLAIMS_FIELD",
    "PROVIDER_SECRETS_FIELD",
    "PROVIDER_TEAM_ID_FIELD",
    "ProviderSettings",
    "ProvidersSettings",
    "module_config",
    "provider_name_value",
    "provider_settings_from_config",
)
