"""Configuration declarations for API response behaviour."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from wybra.config.transforms import to_url_path
from wybra.config.types import ConfigDef, ConfigField, ConfigGroup

ENV_API_PATH_PREFIX: Final = "API_PATH_PREFIX"
ENV_API_PAGING_LINK_MODE: Final = "API_PAGING_LINK_MODE"
DEFAULT_API_PATH_PREFIX: Final = "/api"


class ApiLinkMode(StrEnum):
    PATHLESS = "pathless"
    REQUEST_PATH = "request_path"


def parse_api_link_mode(
    value: ApiLinkMode | str,
    *,
    name: str = "app.api.paging_link_mode",
) -> ApiLinkMode:
    if isinstance(value, ApiLinkMode):
        return value
    if isinstance(value, str):
        try:
            return ApiLinkMode(value)
        except ValueError as exc:
            choices = api_link_mode_choices()
            raise ValueError(f"{name} must be one of: {choices}.") from exc
    raise ValueError(f"{name} must be a string.")


def api_link_mode_choices() -> str:
    return ", ".join(repr(mode.value) for mode in ApiLinkMode)


def _path_prefix_value(value: object) -> str:
    return to_url_path(value, name="app.api.path_prefix")


def _link_mode_value(value: object) -> ApiLinkMode:
    if not isinstance(value, (ApiLinkMode, str)):
        raise ValueError("app.api.paging_link_mode must be a string.")
    return parse_api_link_mode(value)


module_config: Final = ConfigDef(
    {
        "app.api": ConfigGroup(
            fields=(
                ConfigField(
                    name="path_prefix",
                    default=DEFAULT_API_PATH_PREFIX,
                    env=ENV_API_PATH_PREFIX,
                    transform=_path_prefix_value,
                ),
                ConfigField(
                    name="paging_link_mode",
                    default=ApiLinkMode.PATHLESS.value,
                    env=ENV_API_PAGING_LINK_MODE,
                    transform=_link_mode_value,
                ),
            ),
        ),
    }
)


__all__ = [
    "ApiLinkMode",
    "DEFAULT_API_PATH_PREFIX",
    "api_link_mode_choices",
    "module_config",
    "parse_api_link_mode",
]
