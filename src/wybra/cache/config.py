from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup
from wybra.config.transforms import to_non_blank_string, to_optional_non_blank_string

CACHE_CONFIG_SECTION: Final = "cache"
DEFAULT_CACHE_BACKEND: Final = "memory"


def to_cache_backend(value: object) -> str:
    backend = to_non_blank_string(value).lower()
    if backend not in {"memory", "redis"}:
        raise ValueError("cache backend must be 'memory' or 'redis'.")
    return backend


module_config: Final = ConfigDef(
    {
        CACHE_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="backend",
                    default=DEFAULT_CACHE_BACKEND,
                    transform=to_cache_backend,
                ),
                ConfigField(
                    name="url", default=None, transform=to_optional_non_blank_string
                ),
            )
        )
    }
)


__all__ = (
    "CACHE_CONFIG_SECTION",
    "DEFAULT_CACHE_BACKEND",
    "module_config",
    "to_cache_backend",
)
