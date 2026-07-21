from __future__ import annotations

from wybra.cache.capabilities import CacheCapability, InMemoryCache, RedisCache
from wybra.cache.settings import CacheSettings
from wybra.site import Site


async def setup_site(site: Site) -> None:
    settings = CacheSettings.load_settings(site.config)
    capability: CacheCapability
    if settings.backend == "memory":
        capability = InMemoryCache()
    else:
        assert settings.url is not None
        capability = RedisCache(settings.url)
    site.provide_capability(CacheCapability, capability)


__all__ = ("setup_site",)
