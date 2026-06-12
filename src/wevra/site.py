from __future__ import annotations

from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import cast

from fastapi import FastAPI

from wevra.config import AppConfigSource, ConfigService, ConfigSource, FileConfigSource
from wevra.core.composition import AppConfig

ConfigSourceInput = str | PathLike[str] | AppConfig | ConfigSource


@dataclass(frozen=True, slots=True)
class Site:
    app: FastAPI
    config: ConfigService

    @property
    def modules(self) -> tuple[str, ...]:
        app_config = self.config.get_config("app") or {}
        modules = app_config.get("modules", ())
        if isinstance(modules, tuple) and all(
            isinstance(module, str) for module in modules
        ):
            return modules
        if isinstance(modules, list) and all(
            isinstance(module, str) for module in modules
        ):
            return tuple(modules)
        return ()

    def has_module(self, owner: str) -> bool:
        return owner in self.modules


def start(app: FastAPI, *, config_source: ConfigSourceInput) -> Site:
    return Site(
        app=app,
        config=ConfigService([_normalise_config_source(config_source)]),
    )


def _normalise_config_source(config_source: ConfigSourceInput) -> ConfigSource:
    if isinstance(config_source, AppConfig):
        return AppConfigSource(config_source)
    if isinstance(config_source, str):
        return FileConfigSource(Path(config_source))
    if isinstance(config_source, PathLike):
        return FileConfigSource(Path(cast(PathLike[str], config_source)))
    return cast(ConfigSource, config_source)
