from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Self, cast
from urllib.parse import urlparse

from wybra.config import BaseSettings, ConfigDef, ConfigService
from wybra.core.exceptions import ConfigurationError
from wybra.messages.config import (
    DEFAULT_CACHE_KEY_PREFIX,
    DEFAULT_DATABASE_CONNECTION_NAME,
    DEFAULT_MESSAGE_MAX_LENGTH,
    DEFAULT_MESSAGE_TTL_SECONDS,
    DEFAULT_QUEUE_DEPTH,
    MESSAGES_CONFIG_SECTION,
    MessageStorageBackend,
    module_config,
    to_non_blank_string,
    to_optional_non_blank_string,
    to_positive_float,
    to_positive_int,
    to_storage_backend,
)


@dataclass(frozen=True, slots=True)
class MessagesSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = MESSAGES_CONFIG_SECTION

    storage_backend: MessageStorageBackend | str = MessageStorageBackend.SESSION
    queue_depth: int | str = DEFAULT_QUEUE_DEPTH
    message_max_length: int | str = DEFAULT_MESSAGE_MAX_LENGTH
    message_ttl_seconds: float | str = DEFAULT_MESSAGE_TTL_SECONDS
    cache_url: str | None = None
    cache_key_prefix: str = DEFAULT_CACHE_KEY_PREFIX
    database_connection_name: str = DEFAULT_DATABASE_CONNECTION_NAME

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Any],
    ) -> Self:
        return cls(**cls.settings_kwargs(config))

    def __post_init__(self) -> None:
        storage_backend = _configuration_value(
            to_storage_backend,
            self.storage_backend,
            "storage_backend",
        )
        queue_depth = _configuration_value(
            to_positive_int,
            self.queue_depth,
            "queue_depth",
        )
        message_max_length = _configuration_value(
            to_positive_int,
            self.message_max_length,
            "message_max_length",
        )
        message_ttl_seconds = _configuration_value(
            to_positive_float,
            self.message_ttl_seconds,
            "message_ttl_seconds",
        )
        cache_url = _configuration_value(
            to_optional_non_blank_string,
            self.cache_url,
            "cache_url",
        )
        cache_key_prefix = _configuration_value(
            to_non_blank_string,
            self.cache_key_prefix,
            "cache_key_prefix",
        )
        database_connection_name = _configuration_value(
            to_non_blank_string,
            self.database_connection_name,
            "database_connection_name",
        )
        if storage_backend is MessageStorageBackend.CACHE:
            if cache_url is None:
                raise ConfigurationError(
                    "wybra.messages.cache_url is required when "
                    "storage_backend is 'cache'."
                )
            _validate_cache_url(cache_url)

        object.__setattr__(self, "storage_backend", storage_backend)
        object.__setattr__(self, "queue_depth", queue_depth)
        object.__setattr__(self, "message_max_length", message_max_length)
        object.__setattr__(self, "message_ttl_seconds", message_ttl_seconds)
        object.__setattr__(self, "cache_url", cache_url)
        object.__setattr__(self, "cache_key_prefix", cache_key_prefix)
        object.__setattr__(
            self,
            "database_connection_name",
            database_connection_name,
        )

    @property
    def resolved_storage_backend(self) -> MessageStorageBackend:
        return cast(MessageStorageBackend, self.storage_backend)

    @property
    def resolved_queue_depth(self) -> int:
        return cast(int, self.queue_depth)

    @property
    def resolved_message_max_length(self) -> int:
        return cast(int, self.message_max_length)

    @property
    def resolved_message_ttl_seconds(self) -> float:
        return cast(float, self.message_ttl_seconds)


def _configuration_value[ValueT](
    normalise: Any,
    value: object,
    setting_name: str,
) -> ValueT:
    try:
        return normalise(value)
    except ValueError as exc:
        raise ConfigurationError(f"wybra.messages.{setting_name}: {exc}") from exc


def _validate_cache_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme in {"memory", "redis", "rediss"}:
        return
    raise ConfigurationError(
        "wybra.messages.cache_url must use memory://, redis://, or rediss://."
    )


__all__ = ("MessagesSettings",)
