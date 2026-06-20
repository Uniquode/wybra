"""Shared asset configuration models and parsing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from wybra.config.transforms import to_bool, to_raw_path, to_url_path
from wybra.config.types import ConfigDef, ConfigField, ConfigGroup

ENV_STATIC_ROOT: Final = "STATIC_ROOT"
ENV_STATIC_SERVE: Final = "STATIC_SERVE"
ENV_STATIC_URL: Final = "STATIC_URL"
ENV_STATIC_EXPORT_MODE: Final = "STATIC_EXPORT_MODE"
DEFAULT_ASSET_ROOT: Final = Path("static")


class AssetExportMode(StrEnum):
    NORMAL = "normal"


@dataclass(frozen=True, slots=True)
class AssetCorsPolicy:
    allow_origins: tuple[str, ...] = ("*",)
    allow_methods: tuple[str, ...] = ("GET", "HEAD")
    allow_headers: tuple[str, ...] = ()
    expose_headers: tuple[str, ...] = ()
    allow_credentials: bool = False
    max_age: int = 600


@dataclass(frozen=True, slots=True)
class AssetCorsOptions(AssetCorsPolicy):
    enabled: bool = False
    paths: Mapping[str, AssetCorsPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))


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
        "app.assets.cors": ConfigGroup(
            fields=(
                ConfigField(name="enabled", default=False, transform=to_bool),
                ConfigField(name="allow_origins", default=("*",)),
                ConfigField(name="allow_methods", default=("GET", "HEAD")),
                ConfigField(name="allow_headers", default=()),
                ConfigField(name="expose_headers", default=()),
                ConfigField(name="allow_credentials", default=False, transform=to_bool),
                ConfigField(name="max_age", default=600),
            ),
        ),
    }
)


def load_asset_cors_options(
    data: Mapping[str, Any],
    name: str,
    *,
    error_type: type[Exception] = ValueError,
) -> AssetCorsOptions:
    if not data:
        return AssetCorsOptions()

    base = load_asset_cors_policy(data, name, error_type=error_type)
    paths_data = _optional_mapping(data, f"{name}.paths", error_type=error_type)
    paths: dict[str, AssetCorsPolicy] = {}
    for path, path_data in paths_data.items():
        if not isinstance(path, str) or not path.strip():
            raise error_type(f"{name}.paths must contain only non-blank URL path keys.")
        if not isinstance(path_data, Mapping):
            raise error_type(f"{name}.paths.{path} must be a table.")
        paths[normalise_url_path_prefix(path)] = load_asset_cors_policy(
            path_data,
            f"{name}.paths.{path}",
            defaults=base,
            error_type=error_type,
        )

    return AssetCorsOptions(
        enabled=_bool_value(data, f"{name}.enabled", False, error_type=error_type),
        allow_origins=base.allow_origins,
        allow_methods=base.allow_methods,
        allow_headers=base.allow_headers,
        expose_headers=base.expose_headers,
        allow_credentials=base.allow_credentials,
        max_age=base.max_age,
        paths=paths,
    )


def load_asset_cors_policy(
    data: Mapping[str, Any],
    name: str,
    *,
    defaults: AssetCorsPolicy | None = None,
    error_type: type[Exception] = ValueError,
) -> AssetCorsPolicy:
    defaults = defaults or AssetCorsPolicy()
    return AssetCorsPolicy(
        allow_origins=_optional_str_list(
            data,
            f"{name}.allow_origins",
            defaults.allow_origins,
            error_type=error_type,
        ),
        allow_methods=_optional_str_list(
            data,
            f"{name}.allow_methods",
            defaults.allow_methods,
            error_type=error_type,
        ),
        allow_headers=_optional_str_list(
            data,
            f"{name}.allow_headers",
            defaults.allow_headers,
            allow_empty=True,
            error_type=error_type,
        ),
        expose_headers=_optional_str_list(
            data,
            f"{name}.expose_headers",
            defaults.expose_headers,
            allow_empty=True,
            error_type=error_type,
        ),
        allow_credentials=_bool_value(
            data,
            f"{name}.allow_credentials",
            defaults.allow_credentials,
            error_type=error_type,
        ),
        max_age=_optional_non_negative_int(
            data,
            f"{name}.max_age",
            defaults.max_age,
            error_type=error_type,
        ),
    )


def normalise_url_path_prefix(path: str) -> str:
    """Normalise a configured URL prefix and retain a trailing slash."""
    return f"/{path.strip('/')}/" if path.strip("/") else "/"


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


def _optional_mapping(
    data: Mapping[str, Any],
    name: str,
    *,
    error_type: type[Exception],
) -> Mapping[str, Any]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise error_type(f"{name} must be a table.")


def _bool_value(
    data: Mapping[str, Any],
    name: str,
    default: bool,
    *,
    error_type: type[Exception],
) -> bool:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    try:
        return to_bool(value)
    except ValueError as exc:
        raise error_type(f"{name} must be a boolean.") from exc


def _optional_non_negative_int(
    data: Mapping[str, Any],
    name: str,
    default: int,
    *,
    error_type: type[Exception],
) -> int:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, int) and value >= 0:
        return value
    raise error_type(f"{name} must be a non-negative integer.")


def _optional_str_list(
    data: Mapping[str, Any],
    name: str,
    default: tuple[str, ...],
    *,
    allow_empty: bool = False,
    error_type: type[Exception],
) -> tuple[str, ...]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    if (
        isinstance(value, (list, tuple))
        and (allow_empty or value)
        and all(isinstance(item, str) and item.strip() for item in value)
    ):
        return tuple(item.strip() for item in value)
    requirement = "a string list" if allow_empty else "a non-empty string list"
    raise error_type(f"{name} must be {requirement}.")


__all__ = [
    "AssetCorsOptions",
    "AssetCorsPolicy",
    "AssetExportMode",
    "asset_export_mode_choices",
    "load_asset_cors_options",
    "load_asset_cors_policy",
    "normalise_url_path_prefix",
    "parse_asset_export_mode",
]
