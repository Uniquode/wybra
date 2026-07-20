from __future__ import annotations

from wybra.assets import StaticAssetCapability
from wybra.cache import CacheCapability
from wybra.events import EventsCapability
from wybra.site import Site
from wybra.template.capabilities import DefaultTemplateCapability, TemplateCapability
from wybra.template.discovery import template_sources_from_modules
from wybra.template.middleware import register_template_context_middleware
from wybra.template.settings import TemplateSettings


async def setup_site(site: Site) -> None:
    settings = TemplateSettings.load_settings(site.config)
    capability = DefaultTemplateCapability(
        template_root=settings.root,
        template_sources=template_sources_from_modules(site.modules),
        assets=site.capability_proxy(StaticAssetCapability),
        cache_provider=site.capability_proxy(CacheCapability).optional,
        events=site.require_capability(EventsCapability),
        include_request_context=settings.request_context_enabled,
        auto_reload=settings.auto_reload,
        cache_size=settings.cache_size,
    )
    site.provide_capability(TemplateCapability, capability)
    if settings.request_context_enabled:
        register_template_context_middleware(site)


async def post_setup_site(site: Site) -> None:
    capability = site.require_capability(TemplateCapability)
    if (
        isinstance(capability, DefaultTemplateCapability)
        and capability.assets is not None
    ):
        await capability.assets.finalise_optional()


__all__ = ("post_setup_site", "setup_site")
