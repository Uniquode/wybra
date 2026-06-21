"""Reusable module-composed UI support widgets."""

from __future__ import annotations

from wybra.site import Site
from wybra.template import TemplateCapability
from wybra.widgets.config import WidgetsSettings, module_config
from wybra.widgets.features import configure_widgets


async def setup_site(site: Site) -> None:
    settings = WidgetsSettings.load_settings(site.config)
    configure_widgets(settings.features)


async def post_setup_site(site: Site) -> None:
    site.require_capability(TemplateCapability)


__all__ = (
    "WidgetsSettings",
    "module_config",
    "post_setup_site",
    "setup_site",
)
