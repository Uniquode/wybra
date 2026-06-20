from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from importlib import import_module
from inspect import iscoroutinefunction
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, TypeGuard, TypeVar, cast
from urllib.parse import unquote, urlparse

from fastapi import FastAPI

from wybra.config import (
    AppConfigSource,
    ConfigService,
    ConfigSource,
    ConfigSourceError,
    ConfigSourceMetadata,
    FileConfigSource,
)
from wybra.core.composition import (
    APP_CONFIG_ENV,
    APP_ROOT_ENV,
    AppConfig,
    resolve_project_root,
)
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.environment import EnvironmentMapping, load_environment
from wybra.core.settings import load_composition_config_from_environment
from wybra.errors import structured_error, type_name
from wybra.tools.project import runtime_project_root

logger = logging.getLogger(__name__)

ConfigSourceInput = str | AppConfig | ConfigSource | None
ModuleLoader = Callable[[str], ModuleType]
AppT = TypeVar("AppT", bound=FastAPI)
SETUP_SITE_ATTRIBUTE = "setup_site"
T = TypeVar("T")


class SiteLifespan(Protocol[AppT]):
    def __call__(self, app: AppT) -> AbstractAsyncContextManager[None]: ...


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
        """Register a capability under a concrete runtime-checkable type."""
        if capability_type in self._capabilities:
            raise SiteCapabilityError(
                structured_error(
                    "Capability is already provided",
                    capability_type=type_name(capability_type),
                )
            )
        if not _matches_capability_type(value, capability_type):
            raise SiteCapabilityError(
                structured_error(
                    "Capability value has invalid type",
                    capability_type=type_name(capability_type),
                    value_type=type(value).__name__,
                )
            )
        self._capabilities[capability_type] = value

    def require_capability(self, capability_type: type[T]) -> T:
        try:
            capability = self._capabilities[capability_type]
        except KeyError as exc:
            raise SiteCapabilityError(
                structured_error(
                    "Missing capability",
                    capability_type=type_name(capability_type),
                )
            ) from exc
        return cast(T, capability)

    def optional_capability(self, capability_type: type[T]) -> T | None:
        capability = self._capabilities.get(capability_type)
        return cast(T | None, capability)

    def has_capability(self, capability_type: type[object]) -> bool:
        return capability_type in self._capabilities

    def capability_proxy(self, capability_type: type[T]) -> SiteCapabilityProxy[T]:
        """Return a lazy proxy for a capability type."""
        return SiteCapabilityProxy(self, capability_type)

    async def close(self) -> None:
        """Close capabilities that expose an async ``close()`` hook.

        Capability cleanup hooks must be async. Synchronous close hooks are
        invalid.
        """
        if not self._capabilities:
            return

        error_count = 0
        for capability in tuple(self._capabilities.values()):
            close = getattr(capability, "close", None)
            if close is None:
                continue
            if not callable(close) or not iscoroutinefunction(close):
                error_count += 1
                logger.error(
                    "Capability close hook must be async",
                    extra={
                        "capability": type(capability).__name__,
                        "attribute": "close",
                        "attribute_type": type(close).__name__,
                    },
                )
                continue
            try:
                await close()
            except Exception as exc:
                error_count += 1
                logger.exception(
                    "Capability close hook failed",
                    extra={
                        "capability": type(capability).__name__,
                        "attribute": "close",
                        "error_type": type(exc).__name__,
                    },
                )

        self._capabilities.clear()

        if error_count:
            raise SiteCapabilityError(
                structured_error(
                    "Capability close failed",
                    error_count=error_count,
                )
            )


@dataclass(slots=True)
class SiteCapabilityProxy[T]:
    """Lazily resolve a capability from the site and cache the first result.

    The first successful ``require()`` result is cached and reused for the
    lifetime of the proxy. Proxies are therefore immutable once bound; changes to
    registered capabilities at runtime are intentionally not reflected in
    existing proxies.
    """

    site: Site
    capability_type: type[T]
    _capability: T | None = field(default=None, init=False, repr=False)

    def available(self) -> bool:
        return self.site.has_capability(self.capability_type)

    def require(self) -> T:
        if self._capability is None:
            self._capability = self.site.require_capability(self.capability_type)
        return self._capability

    def optional(self) -> T | None:
        if self._capability is not None:
            return self._capability

        capability = self.site.optional_capability(self.capability_type)
        if capability is None:
            return None

        self._capability = capability
        return self._capability

    def __getattr__(self, name: str) -> Any:
        return getattr(self.require(), name)


def start_site(
    *,
    config_source: ConfigSourceInput = None,
    module_loader: ModuleLoader | None = None,
    environ: Mapping[str, str] | None = None,
) -> SiteLifespan[FastAPI]:
    @asynccontextmanager
    async def _start_site(app: FastAPI):
        app.middleware_stack = None
        site = await start(
            app,
            config_source=config_source,
            module_loader=module_loader,
            environ=environ,
        )
        app.state.site = site
        try:
            yield
        finally:
            await site.close()

    return _start_site


def get_site(app: FastAPI) -> Site:
    try:
        site = getattr(app.state, "site", None)
    except AttributeError as exc:
        raise SiteCapabilityError(
            structured_error(
                "Site is not available on app state",
                attribute="state",
            )
        ) from exc
    if not isinstance(site, Site):
        raise SiteCapabilityError(
            structured_error(
                "Site is not available on app state",
                attribute="site",
            )
        )
    return site


async def start(
    app: FastAPI,
    *,
    config_source: ConfigSourceInput = None,
    module_loader: ModuleLoader | None = None,
    environ: Mapping[str, str] | None = None,
) -> Site:
    site = Site(
        app=app,
        config=ConfigService(
            [_normalise_config_source(config_source, environ)],
            config_defs=(RUNTIME_CONFIG_DEF,),
            environ=_startup_environ(config_source, environ),
        ),
    )
    app.state.site = site
    await _setup_modules(site, module_loader or import_module)
    return site


def _startup_environ(
    config_source: ConfigSourceInput,
    environ: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if environ is not None:
        return environ
    project_root = _config_source_project_root(config_source, environ)
    return EnvironmentMapping(load_environment(project_root=project_root))


def _normalise_config_source(
    config_source: ConfigSourceInput,
    environ: Mapping[str, str] | None,
) -> ConfigSource:
    if config_source is None:
        project_root = _startup_project_root(environ)
        env = load_environment(environ=environ, project_root=project_root)
        app_config = load_composition_config_from_environment(
            env,
            project_root=project_root,
            app_config_env=APP_CONFIG_ENV,
            require_app_config=True,
        )
        if app_config is None:  # pragma: no cover - require_app_config prevents this
            raise ConfigSourceError(
                "Application config file could not be resolved; pass --config or set "
                f"{APP_CONFIG_ENV}."
            )
        return AppConfigSource(app_config)
    if isinstance(config_source, AppConfig):
        return AppConfigSource(config_source)
    if isinstance(config_source, str):
        return FileConfigSource(
            _file_config_path(config_source),
            project_root=_explicit_config_project_root(environ),
        )
    if _is_config_source(config_source):
        return config_source
    raise ConfigSourceError(
        "Config source must be a string, AppConfig, or ConfigSource."
    )


def _config_source_project_root(
    config_source: ConfigSourceInput,
    environ: Mapping[str, str] | None,
) -> Path | None:
    if isinstance(config_source, AppConfig):
        return config_source.project_root
    if config_source is None:
        return _startup_project_root(environ)
    return None


def _startup_project_root(environ: Mapping[str, str] | None) -> Path:
    """Resolve the root for default startup discovery.

    Default startup may use the process environment because this is the path
    used by imported ASGI apps after `wybra-runserver` has populated startup
    overrides.
    """
    if environ is not None and APP_ROOT_ENV in environ:
        return resolve_project_root(environ=environ)
    return runtime_project_root()


def _explicit_config_project_root(environ: Mapping[str, str] | None) -> Path:
    """Resolve the root for an explicit config source string.

    Explicit config paths are caller input, so process APP_ROOT must not affect
    them unless the caller also supplied an explicit environment mapping.
    """
    if environ is not None and APP_ROOT_ENV in environ:
        return resolve_project_root(environ=environ)
    return Path.cwd().resolve()


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
    except TypeError as exc:
        raise SiteCapabilityError(
            structured_error(
                "Capability type cannot be runtime-validated",
                capability_type=type_name(capability_type),
                expected="runtime_checkable_type",
            )
        ) from exc


def _require_async_setup_site(module_name: str, setup_site: object) -> None:
    if not callable(setup_site) or not iscoroutinefunction(setup_site):
        raise SiteCapabilityError(
            structured_error(
                "Configured module setup hook is invalid",
                module=module_name,
                attribute="setup_site",
                attribute_type=type(setup_site).__name__,
                expected="async_callable",
            )
        )


async def _setup_modules(site: Site, module_loader: ModuleLoader) -> None:
    for module_name in site.modules:
        module = module_loader(module_name)
        setup_site = getattr(module, SETUP_SITE_ATTRIBUTE, None)
        if setup_site is None:
            continue
        _require_async_setup_site(module_name, setup_site)
        try:
            await setup_site(site)
        except SiteCapabilityError:
            raise
        except Exception as exc:
            raise SiteCapabilityError(
                structured_error(
                    "Configured module setup hook failed",
                    module=module_name,
                    attribute="setup_site",
                    error_type=type(exc).__name__,
                )
            ) from exc
