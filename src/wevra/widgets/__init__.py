"""Reusable module-composed UI support widgets."""

from __future__ import annotations

from wevra.site import Site
from wevra.widgets.config import module_config, widgets_settings_from_config
from wevra.widgets.features import configure_widgets


async def setup_site(site: Site) -> None:
    settings = widgets_settings_from_config(site.config)
    configure_widgets(settings.enabled_features)


__all__ = (
    "module_config",
    "setup_site",
    "widgets_settings_from_config",
)
