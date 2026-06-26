"""Reusable module-composed UI support widgets."""

from __future__ import annotations

from wybra.forms import FormsCapability
from wybra.site import Site
from wybra.template import TemplateCapability
from wybra.widgets.config import THEME_FEATURE, WidgetsSettings, module_config
from wybra.widgets.features import configure_widgets
from wybra.widgets.navigation import (
    DropdownPanel,
    KeyboardShortcut,
    NavigationItem,
    NavigationMenu,
)


async def setup_site(site: Site) -> None:
    settings = WidgetsSettings.load_settings(site.config)
    site.app.state.widgets_settings = settings
    configure_widgets(settings.features)


async def post_setup_site(site: Site) -> None:
    site.require_capability(TemplateCapability)
    settings = site.app.state.widgets_settings
    if THEME_FEATURE in settings.features:
        site.require_capability(FormsCapability)


__all__ = (
    "DropdownPanel",
    "KeyboardShortcut",
    "NavigationItem",
    "NavigationMenu",
    "WidgetsSettings",
    "module_config",
    "post_setup_site",
    "setup_site",
)
