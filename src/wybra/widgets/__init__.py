"""Reusable module-composed UI support widgets."""

from __future__ import annotations

from wybra.site import Site
from wybra.widgets.config import module_config, widgets_settings_from_config
from wybra.widgets.features import configure_widgets


async def setup_site(site: Site) -> None:
    settings = widgets_settings_from_config(site.config)
    configure_widgets(settings.enabled_features)


__all__ = (
    "module_config",
    "setup_site",
    "widgets_settings_from_config",
)
