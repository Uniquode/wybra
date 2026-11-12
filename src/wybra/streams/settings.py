from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from wybra.config import BaseSettings, ConfigDef, ConfigService, CredentialReference
from wybra.config.transforms import to_optional_non_blank_string
from wybra.core.exceptions import ConfigurationError
from wybra.services.secrets import ENVIRONMENT_SOURCE, KEYCHAIN_SOURCE, SecretSource
from wybra.streams.config import (
    DEFAULT_STREAMS_BACKEND,
    DEFAULT_STREAMS_NAMESPACE,
    STREAMS_CONFIG_SECTION,
    module_config,
    to_nats_authentication,
    to_optional_secret_source,
    to_streams_backend,
    to_streams_namespace,
    to_streams_servers,
)


@dataclass(frozen=True, slots=True)
class StreamsSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = STREAMS_CONFIG_SECTION

    backend: str = DEFAULT_STREAMS_BACKEND
    servers: tuple[str, ...] | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    url_source: SecretSource | None = None
    url_key: str | None = None
    credentials_source: SecretSource | None = None
    credentials_key: str | None = None
    authentication: str | None = None
    authentication_source: SecretSource | None = None
    authentication_key: str | None = None
    namespace: str = DEFAULT_STREAMS_NAMESPACE

    @classmethod
    def load_settings(cls, config: ConfigService | Mapping[str, Any]) -> Self:
        return cls(**cls.settings_kwargs(config))

    def __post_init__(self) -> None:
        backend = _configuration_value(to_streams_backend, self.backend, "backend")
        servers = _configuration_value(to_streams_servers, self.servers, "servers")
        url = _configuration_value(to_optional_non_blank_string, self.url, "url")
        url_source = _configuration_value(
            to_optional_secret_source,
            self.url_source,
            "url_source",
        )
        url_key = _configuration_value(
            to_optional_non_blank_string,
            self.url_key,
            "url_key",
        )
        credentials_source = _configuration_value(
            to_optional_secret_source,
            self.credentials_source,
            "credentials_source",
        )
        credentials_key = _configuration_value(
            to_optional_non_blank_string,
            self.credentials_key,
            "credentials_key",
        )
        authentication = _configuration_value(
            to_nats_authentication,
            self.authentication,
            "authentication",
        )
        authentication_source = _configuration_value(
            to_optional_secret_source,
            self.authentication_source,
            "authentication_source",
        )
        authentication_key = _configuration_value(
            to_optional_non_blank_string,
            self.authentication_key,
            "authentication_key",
        )
        namespace = _configuration_value(
            to_streams_namespace,
            self.namespace,
            "namespace",
        )
        _validate_connection_settings(
            backend=backend,
            servers=servers,
            url=url,
            url_source=url_source,
            url_key=url_key,
            credentials_source=credentials_source,
            authentication=authentication,
            authentication_source=authentication_source,
            authentication_key=authentication_key,
        )
        if url_key is not None and url_source is None:
            raise ConfigurationError("streams.url_key requires streams.url_source.")
        if credentials_key is not None and credentials_source is None:
            raise ConfigurationError("streams.credentials_key requires streams.credentials_source.")
        if authentication_key is not None and authentication_source is None:
            raise ConfigurationError("streams.authentication_key requires streams.authentication_source.")
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "servers", servers)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "url_source", url_source)
        object.__setattr__(self, "url_key", url_key)
        object.__setattr__(self, "credentials_source", credentials_source)
        object.__setattr__(self, "credentials_key", credentials_key)
        object.__setattr__(self, "authentication", authentication)
        object.__setattr__(self, "authentication_source", authentication_source)
        object.__setattr__(self, "authentication_key", authentication_key)
        object.__setattr__(self, "namespace", namespace)

    @property
    def url_reference(self) -> tuple[SecretSource, str] | None:
        if self.url_source is None:
            return None
        return (self.url_source, self.url_key or _default_url_key(self.url_source))

    @property
    def credentials_reference(self) -> tuple[SecretSource, str] | None:
        if self.credentials_source is None:
            return None
        return (
            self.credentials_source,
            self.credentials_key or _default_credentials_key(self.credentials_source),
        )

    @property
    def authentication_reference(self) -> tuple[SecretSource, str] | None:
        if self.authentication_source is None or self.authentication is None:
            return None
        return (
            self.authentication_source,
            self.authentication_key
            or _default_authentication_key(
                self.authentication_source,
                self.authentication,
            ),
        )

    @property
    def requires_secret_resolution(self) -> bool:
        return any(
            (
                self.url_reference is not None,
                self.credentials_reference is not None,
                self.authentication_reference is not None,
            )
        )

    def credential_references(self) -> tuple[CredentialReference, ...]:
        references: list[CredentialReference] = []
        if self.url_reference is not None and self.url_reference[0] == KEYCHAIN_SOURCE:
            references.append(
                CredentialReference(
                    name="streams-url",
                    key=self.url_reference[1],
                    owner="streams",
                    description="Configured Redis URL for streams.",
                    source=KEYCHAIN_SOURCE,
                    required=True,
                )
            )
        if self.credentials_reference is not None and self.credentials_reference[0] == KEYCHAIN_SOURCE:
            references.append(
                CredentialReference(
                    name="streams-credentials",
                    key=self.credentials_reference[1],
                    owner="streams",
                    description="Configured Redis credentials for streams.",
                    source=KEYCHAIN_SOURCE,
                    required=True,
                )
            )
        if self.authentication_reference is not None and self.authentication_reference[0] == KEYCHAIN_SOURCE:
            authentication = self.authentication
            assert authentication is not None
            references.append(
                CredentialReference(
                    name=f"streams-nats-{authentication}",
                    key=self.authentication_reference[1],
                    owner="streams",
                    description=(f"Configured NATS {authentication.replace('-', ' ')} authentication for streams."),
                    source=KEYCHAIN_SOURCE,
                    required=True,
                )
            )
        return tuple(references)

    def diagnostics(self) -> StreamsConfigurationDiagnostic:
        return StreamsConfigurationDiagnostic(
            backend=self.backend,
            namespace=self.namespace,
            url_source=(self.url_reference[0] if self.url_reference is not None else None),
            url_key=(self.url_reference[1] if self.url_reference is not None else None),
            credentials_source=(self.credentials_reference[0] if self.credentials_reference is not None else None),
            credentials_key=(self.credentials_reference[1] if self.credentials_reference is not None else None),
            authentication=self.authentication,
            authentication_source=(
                self.authentication_reference[0] if self.authentication_reference is not None else None
            ),
            authentication_key=(
                self.authentication_reference[1] if self.authentication_reference is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StreamsConfigurationDiagnostic:
    backend: str
    namespace: str
    url_source: SecretSource | None = None
    url_key: str | None = None
    credentials_source: SecretSource | None = None
    credentials_key: str | None = None
    authentication: str | None = None
    authentication_source: SecretSource | None = None
    authentication_key: str | None = None
    health: str = "configured"


def _configuration_value[ValueT](
    transform: Any,
    value: object,
    field_name: str,
) -> ValueT:
    try:
        return transform(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"streams.{field_name}: {exc}") from exc


def _validate_connection_settings(
    *,
    backend: str,
    servers: tuple[str, ...] | None,
    url: str | None,
    url_source: SecretSource | None,
    url_key: str | None,
    credentials_source: SecretSource | None,
    authentication: str | None,
    authentication_source: SecretSource | None,
    authentication_key: str | None,
) -> None:
    if backend == "memory":
        if any(
            (
                servers,
                url,
                url_source,
                credentials_source,
                authentication,
                authentication_source,
            )
        ):
            raise ConfigurationError("streams memory backend cannot configure servers, URL, or secrets.")
        return
    if backend == "nats":
        if not servers:
            raise ConfigurationError("streams.servers is required for the NATS backend.")
        if any((url, url_source, credentials_source)):
            raise ConfigurationError("streams NATS backend cannot configure Redis URL or secrets.")
        if authentication is None and authentication_source is not None:
            raise ConfigurationError("streams.authentication_source requires streams.authentication.")
        if authentication is not None and authentication_source is None:
            raise ConfigurationError("streams.authentication requires streams.authentication_source.")
        if authentication_source is not None and authentication_source not in {
            ENVIRONMENT_SOURCE,
            KEYCHAIN_SOURCE,
        }:
            raise ConfigurationError("streams.authentication_source must be 'environment' or 'keychain'.")
        _validate_nats_server_urls(servers)
        return
    if authentication is not None or authentication_source is not None:
        raise ConfigurationError("streams.authentication is only valid for the NATS backend.")
    if servers:
        raise ConfigurationError("streams.servers is only valid for the NATS backend.")
    for source, field_name in (
        (url_source, "url_source"),
        (credentials_source, "credentials_source"),
    ):
        if source is not None and source not in {ENVIRONMENT_SOURCE, KEYCHAIN_SOURCE}:
            raise ConfigurationError(f"streams.{field_name} must be 'environment' or 'keychain'.")
    if url_source is not None:
        if url is not None or credentials_source is not None:
            raise ConfigurationError("streams.url_source cannot be combined with URL or credentials.")
        if url_source == ENVIRONMENT_SOURCE and url_key is None:
            raise ConfigurationError(
                "streams.url_source='environment' requires streams.url_key. "
                "Use the native WYBRA_STREAMS_URL override or select a distinct "
                "environment variable key."
            )
        return
    if url is None:
        raise ConfigurationError("streams.url is required for the Redis backend.")


def _default_url_key(source: SecretSource) -> str:
    return "WYBRA_STREAMS_URL" if source == "environment" else "streams/redis/url"


def _default_credentials_key(source: SecretSource) -> str:
    if source == "environment":
        return "WYBRA_STREAMS_REDIS_CREDENTIALS"
    return "streams/redis/credentials"


def _default_authentication_key(source: SecretSource, authentication: str) -> str:
    if source == ENVIRONMENT_SOURCE:
        if authentication == "token":
            return "WYBRA_STREAMS_NATS_TOKEN"
        return "WYBRA_STREAMS_NATS_CREDENTIALS"
    if authentication == "token":
        return "streams/nats/token"
    return "streams/nats/credentials"


def _validate_nats_server_urls(servers: tuple[str, ...]) -> None:
    websocket_servers = False
    tcp_servers = False
    for server in servers:
        try:
            parsed = urlsplit(server)
            if parsed.scheme.lower() not in {"nats", "tls", "ws", "wss"}:
                raise ValueError
            if parsed.hostname is None:
                raise ValueError
            if parsed.username is not None or parsed.password is not None:
                raise ValueError
            if parsed.port is not None and not 0 < parsed.port <= 65_535:
                raise ValueError
        except ValueError:
            raise ConfigurationError("streams.servers must contain valid NATS server URLs.") from None
        if parsed.scheme.lower() in {"ws", "wss"}:
            websocket_servers = True
        else:
            tcp_servers = True
    if websocket_servers and tcp_servers:
        raise ConfigurationError("streams.servers cannot mix WebSocket and TCP NATS server URLs.")


__all__ = ("StreamsConfigurationDiagnostic", "StreamsSettings")
