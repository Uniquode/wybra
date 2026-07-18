"""Application cache capability."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "CacheCapability": "wybra.cache.capabilities",
    "CacheFactory": "wybra.cache.capabilities",
    "CacheSettings": "wybra.cache.settings",
    "InMemoryCache": "wybra.cache.capabilities",
    "RedisCache": "wybra.cache.capabilities",
    "module_config": "wybra.cache.config",
    "setup_site": "wybra.cache.setup",
}

provides_cache_capability = True


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.cache' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [*sorted(_EXPORT_MODULES), "provides_cache_capability"]
