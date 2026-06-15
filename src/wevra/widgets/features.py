from __future__ import annotations

from dataclasses import dataclass

from wevra.widgets.config import LOGIN_FEATURE, THEME_FEATURE

_enabled_features: tuple[str, ...] = (THEME_FEATURE,)


@dataclass(frozen=True, slots=True)
class WidgetFeature:
    name: str
    route_labels: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    static_assets: tuple[str, ...] = ()


THEME_WIDGET = WidgetFeature(
    name=THEME_FEATURE,
    route_labels=("partials", "api"),
    templates=("layouts/page.html", "components/theme_selector.html"),
    static_assets=("styles/widgets.css",),
)
LOGIN_WIDGET = WidgetFeature(
    name=LOGIN_FEATURE,
    templates=("layouts/page.html", "components/login_control.html"),
    static_assets=("styles/widgets.css",),
)
WIDGET_FEATURES: tuple[WidgetFeature, ...] = (THEME_WIDGET, LOGIN_WIDGET)


def configure_widgets(features: tuple[str, ...]) -> None:
    global _enabled_features
    _enabled_features = features

    from wevra.widgets import context, routes

    context.configure_context(features)
    routes.configure_routes(features)


def feature_enabled(feature: str) -> bool:
    return feature in _enabled_features


def enabled_features() -> tuple[str, ...]:
    return _enabled_features


__all__ = (
    "LOGIN_WIDGET",
    "THEME_WIDGET",
    "WIDGET_FEATURES",
    "WidgetFeature",
    "configure_widgets",
    "enabled_features",
    "feature_enabled",
)
