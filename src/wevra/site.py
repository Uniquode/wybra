from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TypeGuard, TypeVar, cast
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
ModuleLoader = Callable[[str], ModuleType | object | None]
SETUP_SITE_ATTRIBUTE = "setup_site"
T = TypeVar("T")


class SiteCapabilityError(RuntimeError):
    """Raised when a site capability cannot be registered or resolved."""


@dataclass(frozen=True, slots=True)
class Site:
    app: FastAPI
    config: ConfigService
    _capabilities: dict[type[object], object] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

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

    def provide_capability(self, capability_type: type[T], value: T) -> None:
        if capability_type in self._capabilities:
            raise SiteCapabilityError(
                f"Capability {capability_type.__name__} is already provided."
            )
        if not _matches_capability_type(value, capability_type):
            raise SiteCapabilityError(
                f"Capability value for {capability_type.__name__} has invalid type."
            )
        self._capabilities[capability_type] = value

    def require_capability(self, capability_type: type[T]) -> T:
        try:
            capability = self._capabilities[capability_type]
        except KeyError as exc:
            raise SiteCapabilityError(
                f"Missing capability {capability_type.__name__}."
            ) from exc
        return cast(T, capability)

    def has_capability(self, capability_type: type[object]) -> bool:
        return capability_type in self._capabilities


def start(
    app: FastAPI,
    *,
    config_source: ConfigSourceInput,
    module_loader: ModuleLoader | None = None,
) -> Site:
    site = Site(
        app=app,
        config=ConfigService([_normalise_config_source(config_source)]),
    )
    _setup_modules(site, module_loader or import_module)
    return site


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


def _matches_capability_type(value: object, capability_type: type[object]) -> bool:
    try:
        return isinstance(value, capability_type)
    except TypeError:
        return True


def _setup_modules(site: Site, module_loader: ModuleLoader) -> None:
    for module_name in site.modules:
        module = module_loader(module_name)
        if module is None:
            raise SiteCapabilityError(
                f"Configured module {module_name!r} was not found."
            )
        setup_site = getattr(module, SETUP_SITE_ATTRIBUTE, None)
        if setup_site is None:
            continue
        if not callable(setup_site):
            raise SiteCapabilityError(
                f"Configured module {module_name!r} exposes non-callable setup_site."
            )
        try:
            setup_site(site)
        except Exception as exc:
            raise SiteCapabilityError(
                f"Configured module {module_name!r} setup_site failed: {exc}"
            ) from exc
