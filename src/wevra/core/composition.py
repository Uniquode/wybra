"""Application module composition configuration and loading."""

import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from wevra.core.diagnostics import app_config_message, configured_module_import_message

DEFAULT_APP_CONFIG: Final = Path("app.toml")
APP_CONFIG_ENV: Final = "APP_CONFIG"
DEFAULT_STATIC_EXPORT_ROOT: Final = Path("static")


class CompositionError(Exception):
    """Raised when application composition cannot be loaded."""


@dataclass(frozen=True, slots=True)
class RouteOptions:
    prefixes: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TemplateOptions:
    auto_reload: bool
    cache_size: int


@dataclass(frozen=True, slots=True)
class StaticOptions:
    url_path: str
    export_root: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_path: Path
    project_root: Path
    modules: tuple[str, ...]
    routes: RouteOptions
    templates: TemplateOptions
    static: StaticOptions
    database_url: str | None = None
    auth: dict[str, Any] = field(default_factory=dict)


def load_app_config(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    resolved_project_root = (project_root or Path.cwd()).resolve()
    resolved_config_path = _resolve_config_path(
        resolved_project_root,
        config_path,
        environ or os.environ,
    )
    if not resolved_config_path.is_file():
        raise CompositionError(
            app_config_message(resolved_config_path, "does not exist")
        )

    try:
        data = tomllib.loads(resolved_config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise CompositionError(
            app_config_message(resolved_config_path, f"is invalid: {error}")
        ) from error

    app_data = _required_table(data, "app")

    return AppConfig(
        config_path=resolved_config_path,
        project_root=resolved_project_root,
        modules=_required_str_tuple(
            app_data,
            "app.modules",
        ),
        routes=RouteOptions(
            prefixes=_optional_route_prefixes(
                _optional_table(app_data, "app.routes"),
                "app.routes",
            ),
        ),
        templates=_load_template_options(app_data),
        static=_load_static_options(app_data),
        database_url=_optional_str_or_none(app_data, "app.database_url"),
        auth=_optional_table(data, "auth"),
    )


def load_app_config_modules(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    default_modules: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return configured modules, falling back only when no app.toml is present.

    Explicit `config_path` or `APP_CONFIG` values are always honoured and must
    point at a valid config file. The default modules are used only for callers
    that intentionally support installed/default operation without a project
    `app.toml`.
    """
    resolved_project_root = (project_root or Path.cwd()).resolve()
    environment = environ if environ is not None else os.environ
    if (
        default_modules is not None
        and config_path is None
        and not environment.get(APP_CONFIG_ENV)
        and not (resolved_project_root / DEFAULT_APP_CONFIG).is_file()
    ):
        return tuple(default_modules)

    return load_app_config(
        project_root=resolved_project_root,
        config_path=config_path,
        environ=environment,
    ).modules


def load_modules(module_names: Iterable[str]) -> tuple[ModuleType, ...]:
    modules: list[ModuleType] = []
    for module_name in module_names:
        try:
            modules.append(import_module(module_name))
        except ImportError as error:
            raise CompositionError(
                configured_module_import_message(module_name)
            ) from error

    return tuple(modules)


def _resolve_config_path(
    project_root: Path,
    config_path: Path | None,
    environ: Mapping[str, str],
) -> Path:
    env_config_path = environ.get(APP_CONFIG_ENV)
    path = config_path or (
        Path(env_config_path) if env_config_path else DEFAULT_APP_CONFIG
    )
    if not path.is_absolute():
        path = project_root / path

    return path.resolve()


def _required_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if isinstance(value, dict):
        return value

    raise CompositionError(f"App config must contain a [{name}] table.")


def _optional_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value

    raise CompositionError(f"App config {name} must be a table.")


def _required_str_tuple(data: dict[str, Any], name: str) -> tuple[str, ...]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise CompositionError(f"App config {name} must be a non-empty string list.")

    if not all(isinstance(item, str) and item.strip() for item in value):
        raise CompositionError(
            f"App config {name} must contain only non-blank strings."
        )

    return tuple(value)


def _required_str(data: dict[str, Any], name: str) -> str:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value

    raise CompositionError(f"App config {name} must be a non-blank string.")


def _optional_str(data: dict[str, Any], name: str, default: str) -> str:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip():
        return value

    raise CompositionError(f"App config {name} must be a non-blank string.")


def _optional_str_or_none(data: dict[str, Any], name: str) -> str | None:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value

    raise CompositionError(f"App config {name} must be a non-blank string.")


def _required_bool(data: dict[str, Any], name: str) -> bool:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if isinstance(value, bool):
        return value

    raise CompositionError(f"App config {name} must be a boolean.")


def _required_non_negative_int(data: dict[str, Any], name: str) -> int:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if isinstance(value, int) and value >= 0:
        return value

    raise CompositionError(f"App config {name} must be a non-negative integer.")


def _optional_route_prefixes(
    data: dict[str, Any],
    name: str,
) -> dict[str, dict[str, str]]:
    prefixes: dict[str, dict[str, str]] = {}
    for configured_module_name, module_routes in data.items():
        if (
            not isinstance(configured_module_name, str)
            or not configured_module_name.strip()
        ):
            raise CompositionError(
                f"App config {name} must contain only non-blank module names."
            )
        module_name = _normalise_route_module_key(configured_module_name)
        if module_name in prefixes:
            raise CompositionError(
                f"App config {name} contains duplicate route entries for "
                f"module {module_name!r}."
            )
        if not isinstance(module_routes, dict):
            raise CompositionError(
                f"App config {name}.{configured_module_name} must be a table "
                "of router labels."
            )

        prefixes[module_name] = _route_label_prefixes(
            module_routes,
            f"{name}.{configured_module_name}",
        )

    return prefixes


def _normalise_route_module_key(module_name: str) -> str:
    return module_name.replace("-", ".")


def _route_label_prefixes(
    data: dict[str, Any],
    name: str,
) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for label, prefix in data.items():
        if not isinstance(label, str) or not label.strip():
            raise CompositionError(
                f"App config {name} must contain only non-blank router labels."
            )
        if not isinstance(prefix, str):
            raise CompositionError(
                f"App config {name}.{label} must be a string prefix."
            )

        prefixes[label] = prefix

    return prefixes


def _load_template_options(data: dict[str, Any]) -> TemplateOptions:
    template_data = _required_table(data, "app.templates")
    return TemplateOptions(
        auto_reload=_required_bool(
            template_data,
            "app.templates.auto_reload",
        ),
        cache_size=_required_non_negative_int(
            template_data,
            "app.templates.cache_size",
        ),
    )


def _load_static_options(data: dict[str, Any]) -> StaticOptions:
    static_data = _required_table(data, "app.static")
    return StaticOptions(
        url_path=_required_str(static_data, "app.static.url_path"),
        export_root=Path(
            _optional_str(
                static_data,
                "app.static.export_root",
                DEFAULT_STATIC_EXPORT_ROOT.as_posix(),
            )
        ),
    )


__all__ = [
    "AppConfig",
    "APP_CONFIG_ENV",
    "CompositionError",
    "DEFAULT_APP_CONFIG",
    "DEFAULT_STATIC_EXPORT_ROOT",
    "RouteOptions",
    "StaticOptions",
    "TemplateOptions",
    "load_app_config",
    "load_app_config_modules",
    "load_modules",
]
