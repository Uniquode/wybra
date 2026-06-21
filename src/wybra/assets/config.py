"""Shared asset configuration models and parsing helpers."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from wybra.config.transforms import to_bool, to_raw_path, to_url_path
from wybra.config.types import ConfigDef, ConfigField, ConfigGroup

ENV_STATIC_ROOT: Final = "STATIC_ROOT"
ENV_STATIC_SERVE: Final = "STATIC_SERVE"
ENV_STATIC_URL: Final = "STATIC_URL"
ENV_STATIC_EXPORT_MODE: Final = "STATIC_EXPORT_MODE"
DEFAULT_ASSET_ROOT: Final = Path("static")


class AssetExportMode(StrEnum):
    NORMAL = "normal"


def _path_value(value: object, *, name: str = "app.assets.root") -> Path:
    return to_raw_path(value, name=name)


def _export_mode_value(value: object) -> AssetExportMode:
    if not isinstance(value, AssetExportMode | str):
        raise ValueError("app.assets.export_mode must be a string.")
    try:
        return parse_asset_export_mode(value, name="app.assets.export_mode")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _url_path_value(value: object, *, name: str = "app.assets.url_path") -> str:
    return to_url_path(value, name=name)


module_config: Final = ConfigDef(
    {
        "app.assets": ConfigGroup(
            fields=(
                ConfigField(name="url_path", default="/static/", env=ENV_STATIC_URL),
                ConfigField(
                    name="root",
                    default=DEFAULT_ASSET_ROOT,
                    env=ENV_STATIC_ROOT,
                    transform=_path_value,
                ),
                ConfigField(
                    name="export_mode",
                    default=AssetExportMode.NORMAL.value,
                    env=ENV_STATIC_EXPORT_MODE,
                    transform=_export_mode_value,
                ),
                ConfigField(
                    name="serve",
                    default=True,
                    env=ENV_STATIC_SERVE,
                    transform=to_bool,
                ),
            ),
        ),
    }
)


def parse_asset_export_mode(
    value: AssetExportMode | str,
    *,
    name: str = "app.assets.export_mode",
) -> AssetExportMode:
    if isinstance(value, AssetExportMode):
        return value
    if isinstance(value, str):
        try:
            return AssetExportMode(value)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be one of: {asset_export_mode_choices()}."
            ) from exc
    raise ValueError(f"{name} must be a string.")


def asset_export_mode_choices() -> str:
    return ", ".join(repr(mode.value) for mode in AssetExportMode)


__all__ = [
    "AssetExportMode",
    "asset_export_mode_choices",
    "parse_asset_export_mode",
]
