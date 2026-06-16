from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from wybra.config import ConfigDef, ConfigField, ConfigGroup
from wybra.config.types import ConfigSourceError

THEME_FEATURE: Final = "theme"
LOGIN_FEATURE: Final = "login"
WIDGETS_CONFIG_SECTION: Final = "wybra.widgets"
WIDGET_FEATURES: Final = frozenset({LOGIN_FEATURE, THEME_FEATURE})
DEFAULT_WIDGET_FEATURES: Final = (THEME_FEATURE, LOGIN_FEATURE)


@dataclass(frozen=True, slots=True)
class WidgetsSettings:
    enabled_features: tuple[str, ...] = DEFAULT_WIDGET_FEATURES


class WidgetsConfigProvider(Protocol):
    def get_config(self, section: str) -> Mapping[str, Any] | None: ...


def to_widget_features(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        features = tuple(
            feature.strip() for feature in value.split(",") if feature.strip()
        )
        return _validate_widget_features(features)
    if isinstance(value, list | tuple):
        return _validate_widget_features(tuple(value))
    raise ValueError("must be a list, tuple, or comma-separated string.")


def widgets_settings_from_config(
    config: WidgetsConfigProvider,
    *,
    default_features: tuple[str, ...] = DEFAULT_WIDGET_FEATURES,
) -> WidgetsSettings:
    values = config.get_config(WIDGETS_CONFIG_SECTION) or {}
    features = values.get("features", default_features)
    try:
        enabled_features = to_widget_features(features)
    except ValueError as exc:
        raise ConfigSourceError(
            f"Config value wybra.widgets.features is invalid: {exc}"
        ) from exc
    return WidgetsSettings(enabled_features=enabled_features)


def _validate_widget_features(features: tuple[object, ...]) -> tuple[str, ...]:
    invalid_types = tuple(
        feature for feature in features if not isinstance(feature, str)
    )
    if invalid_types:
        raise ValueError("feature names must be strings.")
    feature_names = tuple(feature for feature in features if isinstance(feature, str))
    unknown = tuple(sorted(set(feature_names) - WIDGET_FEATURES))
    if unknown:
        raise ValueError("unknown widget feature(s): " + ", ".join(unknown))
    return feature_names


module_config: Final = ConfigDef(
    {
        WIDGETS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="features",
                    default=DEFAULT_WIDGET_FEATURES,
                    transform=to_widget_features,
                ),
            ),
        ),
    }
)

__all__ = (
    "DEFAULT_WIDGET_FEATURES",
    "LOGIN_FEATURE",
    "THEME_FEATURE",
    "WIDGETS_CONFIG_SECTION",
    "WIDGET_FEATURES",
    "WidgetsSettings",
    "module_config",
    "to_widget_features",
    "widgets_settings_from_config",
)
