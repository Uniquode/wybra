"""Application module composition configuration and loading."""

import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from wybra.config.transforms import to_bool
from wybra.core.diagnostics import app_config_message, configured_module_import_message

APP_ROOT_ENV: Final = "APP_ROOT"
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
    root: Path | None = None


@dataclass(frozen=True, slots=True)
class StaticOptions:
    url_path: str
    root: Path | None
    export_root: Path
    serve: bool = True


@dataclass(frozen=True, slots=True)
class RunserverOptions:
    asgi_app: str | None = None
    reload_env: str | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_path: Path
    project_root: Path
    modules: tuple[str, ...]
    routes: RouteOptions
    templates: TemplateOptions
    static: StaticOptions
    runserver: RunserverOptions = field(default_factory=RunserverOptions)
    database_url: str | None = None
    deployment_environment: str | None = None
    auth: dict[str, Any] = field(default_factory=dict)
    raw_config: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def load_app_config(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    resolved_project_root = resolve_project_root(project_root, environ)
    resolved_config_path = _resolve_config_path(
        resolved_project_root,
        config_path,
        environ if environ is not None else os.environ,
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
        runserver=_load_runserver_options(app_data),
        database_url=_optional_str_or_none(app_data, "app.database_url"),
        deployment_environment=_optional_str_or_none(
            app_data,
            "app.deployment_environment",
        ),
        auth=_optional_table(data, "auth"),
        raw_config=_raw_config_sections(data),
    )


def load_app_config_modules(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return configured modules from an explicit config file source."""
    resolved_project_root = resolve_project_root(project_root, environ)
    environment = environ if environ is not None else os.environ

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
    raw_env_config_path = environ.get(APP_CONFIG_ENV)
    if config_path is None and raw_env_config_path is None:
        raise CompositionError(
            "Application config file could not be resolved; pass --config or set "
            f"{APP_CONFIG_ENV}."
        )
    env_config_path = raw_env_config_path.strip() if raw_env_config_path else None
    if config_path is None and not env_config_path:
        raise CompositionError(f"{APP_CONFIG_ENV} must not be blank.")
    if config_path is not None:
        path = config_path
    else:
        assert env_config_path is not None
        path = Path(env_config_path)
    if not path.is_absolute():
        path = project_root / path

    return path.resolve()


def resolve_project_root(
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if project_root is not None:
        return project_root.resolve()

    environment = environ if environ is not None else os.environ
    root_value = environment.get(APP_ROOT_ENV)
    if root_value is None:
        return Path.cwd().resolve()
    if not root_value.strip():
        raise CompositionError(f"{APP_ROOT_ENV} must not be blank.")

    path = Path(root_value)
    if not path.is_absolute():
        path = Path.cwd() / path
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


def _raw_config_sections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {}
    app_data = data.get("app")
    if isinstance(app_data, dict):
        sections["app"] = {
            key: value for key, value in app_data.items() if not isinstance(value, dict)
        }
        for nested_name in ("routes", "runserver", "static", "templates"):
            nested_value = app_data.get(nested_name)
            if isinstance(nested_value, dict):
                sections[f"app.{nested_name}"] = dict(nested_value)

    auth_data = data.get("auth")
    if isinstance(auth_data, dict):
        sections["auth"] = {
            key: value
            for key, value in auth_data.items()
            if not isinstance(value, dict)
        }
        for nested_name, nested_value in auth_data.items():
            if isinstance(nested_value, dict):
                sections[f"auth.{nested_name}"] = dict(nested_value)
    log_data = data.get("log")
    if isinstance(log_data, dict):
        sections["log"] = dict(log_data)
    return sections


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
        if any(character.isspace() for character in configured_module_name):
            raise CompositionError(
                f"App config {name} module name {configured_module_name!r} "
                "must not contain whitespace."
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
        root=(
            Path(root)
            if (root := _optional_str_or_none(template_data, "app.templates.root"))
            else None
        ),
    )


def _load_static_options(data: dict[str, Any]) -> StaticOptions:
    static_data = _required_table(data, "app.static")
    return StaticOptions(
        url_path=_required_str(static_data, "app.static.url_path"),
        root=(
            Path(root)
            if (root := _optional_str_or_none(static_data, "app.static.root"))
            else None
        ),
        export_root=Path(
            _optional_str(
                static_data,
                "app.static.export_root",
                DEFAULT_STATIC_EXPORT_ROOT.as_posix(),
            )
        ),
        serve=_bool_from_config(static_data, "app.static.serve", True),
    )


def _load_runserver_options(data: dict[str, Any]) -> RunserverOptions:
    runserver_data = _optional_table(data, "app.runserver")
    return RunserverOptions(
        asgi_app=_optional_str_or_none(runserver_data, "app.runserver.asgi_app"),
        reload_env=_optional_str_or_none(runserver_data, "app.runserver.reload_env"),
    )


def _bool_from_config(data: dict[str, Any], name: str, default: bool) -> bool:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    try:
        return to_bool(value)
    except ValueError as exc:
        raise CompositionError(f"App config {name} must be a boolean.") from exc


__all__ = [
    "AppConfig",
    "APP_CONFIG_ENV",
    "APP_ROOT_ENV",
    "CompositionError",
    "DEFAULT_STATIC_EXPORT_ROOT",
    "RouteOptions",
    "RunserverOptions",
    "StaticOptions",
    "TemplateOptions",
    "load_app_config",
    "load_app_config_modules",
    "load_modules",
    "resolve_project_root",
]
