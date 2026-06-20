from __future__ import annotations

from wybra.assets import StaticAssetCapability
from wybra.site import Site
from wybra.template.capabilities import DefaultTemplateCapability, TemplateCapability
from wybra.template.discovery import template_sources_from_modules
from wybra.template.middleware import register_template_context_middleware
from wybra.template.settings import TemplateSettings
from wybra.web.csrf import CsrfSettings
from wybra.web.forms.csrf import CsrfProtector


async def setup_site(site: Site) -> None:
    settings = TemplateSettings.load_settings(site.config)
    csrf = getattr(site.app.state, "csrf", None)
    if csrf is None:
        csrf = CsrfSettings.load_settings(site.config).protector()
        site.app.state.csrf = csrf
    elif not isinstance(csrf, CsrfProtector):
        raise RuntimeError("CSRF protector is not configured correctly.")

    capability = DefaultTemplateCapability(
        template_root=settings.root,
        csrf=csrf,
        template_sources=template_sources_from_modules(site.modules),
        assets=site.capability_proxy(StaticAssetCapability),
        include_request_context=settings.request_context_enabled,
        auto_reload=settings.auto_reload,
        cache_size=settings.cache_size,
    )
    site.provide_capability(TemplateCapability, capability)
    if settings.request_context_enabled:
        register_template_context_middleware(site)


__all__ = ("setup_site",)
