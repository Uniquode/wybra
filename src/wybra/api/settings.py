"""Typed settings for API response behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from wybra.api.config import ApiLinkMode, module_config, parse_api_link_mode
from wybra.config import BaseSettings
from wybra.config.transforms import to_url_path
from wybra.config.types import ConfigDef


@dataclass(frozen=True, slots=True)
class ApiSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = "app.api"

    path_prefix: str = "/api"
    paging_link_mode: ApiLinkMode = ApiLinkMode.PATHLESS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path_prefix",
            to_url_path(self.path_prefix, name="app.api.path_prefix"),
        )
        object.__setattr__(
            self,
            "paging_link_mode",
            parse_api_link_mode(self.paging_link_mode),
        )


__all__ = ("ApiSettings",)
