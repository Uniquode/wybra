from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from wybra.cache.config import CACHE_CONFIG_SECTION, module_config, to_cache_backend
from wybra.config import BaseSettings, ConfigDef, ConfigService
from wybra.core.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class CacheSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = CACHE_CONFIG_SECTION

    backend: str = "memory"
    url: str | None = None

    @classmethod
    def load_settings(cls, config: ConfigService | Mapping[str, Any]) -> Self:
        return cls(**cls.settings_kwargs(config))

    def __post_init__(self) -> None:
        try:
            backend = to_cache_backend(self.backend)
        except ValueError as exc:
            raise ConfigurationError(f"cache.backend: {exc}") from exc
        if backend == "redis" and self.url is None:
            raise ConfigurationError("cache.url is required when backend is 'redis'.")
        object.__setattr__(self, "backend", backend)


__all__ = ("CacheSettings",)
