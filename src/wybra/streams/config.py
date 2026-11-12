from __future__ import annotations

import re
from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup
from wybra.config.transforms import to_non_blank_string, to_optional_non_blank_string
from wybra.services.secrets import SecretsError, SecretSource, normalise_secret_source

STREAMS_CONFIG_SECTION: Final = "streams"
DEFAULT_STREAMS_BACKEND: Final = "memory"
DEFAULT_STREAMS_NAMESPACE: Final = "default"
ENV_STREAMS_BACKEND: Final = "WYBRA_STREAMS_BACKEND"
ENV_STREAMS_NAMESPACE: Final = "WYBRA_STREAMS_NAMESPACE"
ENV_STREAMS_SERVERS: Final = "WYBRA_STREAMS_SERVERS"
ENV_STREAMS_URL: Final = "WYBRA_STREAMS_URL"
ENV_STREAMS_URL_SOURCE: Final = "WYBRA_STREAMS_URL_SOURCE"
ENV_STREAMS_URL_KEY: Final = "WYBRA_STREAMS_URL_KEY"
ENV_STREAMS_CREDENTIALS_SOURCE: Final = "WYBRA_STREAMS_CREDENTIALS_SOURCE"
ENV_STREAMS_CREDENTIALS_KEY: Final = "WYBRA_STREAMS_CREDENTIALS_KEY"
ENV_STREAMS_AUTHENTICATION: Final = "WYBRA_STREAMS_AUTHENTICATION"
ENV_STREAMS_AUTHENTICATION_SOURCE: Final = "WYBRA_STREAMS_AUTHENTICATION_SOURCE"
ENV_STREAMS_AUTHENTICATION_KEY: Final = "WYBRA_STREAMS_AUTHENTICATION_KEY"
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def to_streams_backend(value: object) -> str:
    backend = to_non_blank_string(value).lower()
    if backend not in {"memory", "nats", "redis"}:
        raise ValueError("streams backend must be 'memory', 'nats', or 'redis'.")
    return backend


def to_streams_servers(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, str | list | tuple):
        raise ValueError("streams servers must be a list, tuple, or comma-separated string.")
    values = tuple(value.split(",")) if isinstance(value, str) else tuple(value)
    return tuple(to_non_blank_string(item) for item in values)


def to_streams_namespace(value: object) -> str:
    namespace = to_non_blank_string(value)
    if len(namespace) > 64 or _NAMESPACE_PATTERN.fullmatch(namespace) is None:
        raise ValueError(
            "streams namespace must start with a lower-case letter, contain only "
            "lower-case letters, numbers, and underscores, and be at most 64 "
            "characters."
        )
    return namespace


def to_nats_authentication(value: object) -> str | None:
    authentication = to_optional_non_blank_string(value)
    if authentication is None:
        return None
    authentication = authentication.lower()
    if authentication not in {"token", "user-password"}:
        raise ValueError("streams authentication must be 'token' or 'user-password'.")
    return authentication


def to_optional_secret_source(value: object) -> SecretSource | None:
    source = to_optional_non_blank_string(value)
    if source is None:
        return None
    try:
        return normalise_secret_source(source, name="streams secret source")
    except (SecretsError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


module_config: Final = ConfigDef(
    {
        STREAMS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="backend",
                    default=DEFAULT_STREAMS_BACKEND,
                    env=ENV_STREAMS_BACKEND,
                    transform=to_streams_backend,
                ),
                ConfigField(
                    name="servers",
                    env=ENV_STREAMS_SERVERS,
                    transform=to_streams_servers,
                ),
                ConfigField(
                    name="url",
                    env=ENV_STREAMS_URL,
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="url_source",
                    env=ENV_STREAMS_URL_SOURCE,
                    transform=to_optional_secret_source,
                ),
                ConfigField(
                    name="url_key",
                    env=ENV_STREAMS_URL_KEY,
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="credentials_source",
                    env=ENV_STREAMS_CREDENTIALS_SOURCE,
                    transform=to_optional_secret_source,
                ),
                ConfigField(
                    name="credentials_key",
                    env=ENV_STREAMS_CREDENTIALS_KEY,
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="authentication",
                    env=ENV_STREAMS_AUTHENTICATION,
                    transform=to_nats_authentication,
                ),
                ConfigField(
                    name="authentication_source",
                    env=ENV_STREAMS_AUTHENTICATION_SOURCE,
                    transform=to_optional_secret_source,
                ),
                ConfigField(
                    name="authentication_key",
                    env=ENV_STREAMS_AUTHENTICATION_KEY,
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="namespace",
                    default=DEFAULT_STREAMS_NAMESPACE,
                    env=ENV_STREAMS_NAMESPACE,
                    transform=to_streams_namespace,
                ),
            )
        )
    }
)


__all__ = (
    "DEFAULT_STREAMS_BACKEND",
    "DEFAULT_STREAMS_NAMESPACE",
    "STREAMS_CONFIG_SECTION",
    "module_config",
    "to_nats_authentication",
    "to_optional_secret_source",
    "to_streams_backend",
    "to_streams_namespace",
    "to_streams_servers",
)
