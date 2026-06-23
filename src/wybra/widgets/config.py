from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from wybra.config import BaseSettings, ConfigDef, ConfigField, ConfigGroup, to_bool

THEME_FEATURE: Final = "theme"
LOGIN_FEATURE: Final = "login"
WIDGETS_CONFIG_SECTION: Final = "wybra.widgets"
WIDGET_FEATURES: Final = frozenset({LOGIN_FEATURE, THEME_FEATURE})
DEFAULT_WIDGET_FEATURES: Final = (THEME_FEATURE, LOGIN_FEATURE)


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
                ConfigField(
                    name="default_profile_avatar_navigation",
                    default=True,
                    transform=to_bool,
                ),
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class WidgetsSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config

    features: tuple[str, ...] = DEFAULT_WIDGET_FEATURES
    default_profile_avatar_navigation: bool = True


__all__ = (
    "DEFAULT_WIDGET_FEATURES",
    "LOGIN_FEATURE",
    "THEME_FEATURE",
    "WIDGETS_CONFIG_SECTION",
    "WIDGET_FEATURES",
    "WidgetsSettings",
    "module_config",
    "to_widget_features",
)
