from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wevra.core.composition import (
    AppConfig,
    CompositionError,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)
from wevra.site import Site


def app_config_from_site(site: Site) -> AppConfig:
    app_config = _section(site, "app")
    route_config = _section(site, "app.routes")
    template_config = _section(site, "app.templates")
    static_config = _section(site, "app.static")

    project_root = _path_value(app_config, "project_root", Path.cwd())
    return AppConfig(
        config_path=_path_value(app_config, "config_path", project_root / "app.toml"),
        project_root=project_root,
        modules=site.modules,
        routes=RouteOptions(prefixes=_route_prefixes(route_config)),
        templates=TemplateOptions(
            auto_reload=_bool_value(template_config, "auto_reload", True),
            cache_size=_int_value(template_config, "cache_size", 0),
            root=_optional_path_value(template_config, "root"),
        ),
        static=StaticOptions(
            url_path=_str_value(static_config, "url_path", "/static/"),
            root=_optional_path_value(static_config, "root"),
            export_root=_path_value(static_config, "export_root", Path("static")),
            serve=_bool_value(static_config, "serve", True),
        ),
        database_url=_optional_str_value(app_config, "database_url"),
        deployment_environment=_optional_str_value(
            app_config,
            "deployment_environment",
        ),
        auth=dict(_section(site, "auth")),
    )


def _section(site: Site, name: str) -> Mapping[str, Any]:
    value = site.config.get_config(name)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise CompositionError(f"Config section {name!r} must be a mapping.")


def _route_prefixes(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    value = config.get("prefixes", {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CompositionError(
            "Config section 'app.routes' prefixes must be a mapping."
        )

    prefixes: dict[str, dict[str, str]] = {}
    for module_name, route_prefixes in value.items():
        if not isinstance(module_name, str) or not module_name.strip():
            raise CompositionError(
                "Config section 'app.routes' prefixes must contain only "
                "non-blank module names."
            )
        if not isinstance(route_prefixes, Mapping):
            raise CompositionError(
                "Config section 'app.routes' prefixes for "
                f"{module_name!r} must be a mapping."
            )

        prefixes[module_name] = _route_label_prefixes(module_name, route_prefixes)

    return prefixes


def _route_label_prefixes(
    module_name: str,
    config: Mapping[str, Any],
) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for label, prefix in config.items():
        if not isinstance(label, str) or not label.strip():
            raise CompositionError(
                "Config section 'app.routes' prefixes for "
                f"{module_name!r} must contain only non-blank router labels."
            )
        if not isinstance(prefix, str):
            raise CompositionError(
                "Config section 'app.routes' prefix for "
                f"{module_name!r} router {label!r} must be a string."
            )

        prefixes[label] = prefix

    return prefixes


def _path_value(config: Mapping[str, Any], key: str, default: Path) -> Path:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise CompositionError(f"Config value {key!r} must be a non-blank path.")


def _optional_path_value(config: Mapping[str, Any], key: str) -> Path | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise CompositionError(f"Config value {key!r} must be a non-blank path.")


def _bool_value(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise CompositionError(f"Config value {key!r} must be a boolean.")


def _int_value(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, int) and value >= 0:
        return value
    raise CompositionError(f"Config value {key!r} must be a non-negative integer.")


def _str_value(config: Mapping[str, Any], key: str, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip():
        return value
    raise CompositionError(f"Config value {key!r} must be a non-blank string.")


def _optional_str_value(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise CompositionError(f"Config value {key!r} must be a non-blank string.")
