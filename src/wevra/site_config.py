from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wevra.core.composition import (
    AppConfig,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)
from wevra.site import Site


def app_config_from_site(site: Site) -> AppConfig:
    app_config = site.config.get_config("app") or {}
    route_config = site.config.get_config("app.routes") or {}
    template_config = site.config.get_config("app.templates") or {}
    static_config = site.config.get_config("app.static") or {}

    project_root = _path_value(app_config, "project_root", Path.cwd())
    return AppConfig(
        config_path=_path_value(app_config, "config_path", project_root / "app.toml"),
        project_root=project_root,
        modules=site.modules,
        routes=RouteOptions(prefixes=_route_prefixes(route_config)),
        templates=TemplateOptions(
            auto_reload=_bool_value(template_config, "auto_reload", True),
            cache_size=_int_value(template_config, "cache_size", 0),
        ),
        static=StaticOptions(
            url_path=_str_value(static_config, "url_path", "/static/"),
            export_root=_path_value(static_config, "export_root", Path("static")),
        ),
        database_url=_optional_str_value(app_config, "database_url"),
        auth=dict(site.config.get_config("auth") or {}),
    )


def _route_prefixes(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    value = config.get("prefixes", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(module_name): {
            str(label): str(prefix) for label, prefix in route_prefixes.items()
        }
        for module_name, route_prefixes in value.items()
        if isinstance(route_prefixes, dict)
    }


def _path_value(config: Mapping[str, Any], key: str, default: Path) -> Path:
    value = config.get(key)
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    return default


def _bool_value(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    return value if isinstance(value, bool) else default


def _int_value(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key)
    return value if isinstance(value, int) else default


def _str_value(config: Mapping[str, Any], key: str, default: str) -> str:
    value = config.get(key)
    return value if isinstance(value, str) and value.strip() else default


def _optional_str_value(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value.strip() else None
