from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard
from urllib.parse import unquote, urlparse

from fastapi import FastAPI

from wevra.config import (
    AppConfigSource,
    ConfigService,
    ConfigSource,
    ConfigSourceError,
    ConfigSourceMetadata,
    FileConfigSource,
)
from wevra.core.composition import AppConfig

ConfigSourceInput = str | AppConfig | ConfigSource


@dataclass(frozen=True, slots=True)
class Site:
    app: FastAPI
    config: ConfigService

    @property
    def modules(self) -> tuple[str, ...]:
        app_config = self.config.get_config("app") or {}
        modules = app_config.get("modules", ())
        if isinstance(modules, list | tuple) and all(
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
        return FileConfigSource(_file_config_path(config_source))
    if _is_config_source(config_source):
        return config_source
    raise ConfigSourceError(
        "Config source must be a string, AppConfig, or ConfigSource."
    )


def _file_config_path(config_source: str) -> Path:
    value = config_source.strip()
    if not value:
        raise ConfigSourceError("Config source string must not be blank.")

    if _is_windows_absolute_path(value):
        return Path(value)

    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        raise ConfigSourceError(
            f"Unsupported config source URI scheme: {parsed.scheme}."
        )
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ConfigSourceError(
                "file:// config source URI must refer to a local file."
            )
        if not parsed.path:
            raise ConfigSourceError("file:// config source URI must include a path.")
        return Path(unquote(parsed.path))

    return Path(value)


def _is_config_source(value: object) -> TypeGuard[ConfigSource]:
    return callable(getattr(value, "load", None)) and isinstance(
        getattr(value, "metadata", None),
        ConfigSourceMetadata,
    )


def _is_windows_absolute_path(value: str) -> bool:
    return re.match(r"^[A-Za-z]:(?:\\|/)", value) is not None
