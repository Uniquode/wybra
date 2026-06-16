from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wybra.core.composition import (
    AppConfig,
    load_app_config,
)
from wybra.tools.project import ProjectToolConfigurationError


@dataclass(frozen=True, slots=True)
class ConfiguredAppStartup:
    app_target: str
    reload_env_var: str


def resolve_configured_app_startup(
    *,
    project_root: Path,
    config_source: str | None,
) -> ConfiguredAppStartup:
    app_config = load_required_app_config(
        project_root=project_root,
        config_source=config_source,
    )
    app_target = _required_runserver_value(
        app_config.runserver.asgi_app,
        "[app.runserver].asgi_app",
    )
    reload_env_var = _required_runserver_value(
        app_config.runserver.reload_env,
        "[app.runserver].reload_env",
    )
    return ConfiguredAppStartup(
        app_target=app_target,
        reload_env_var=reload_env_var,
    )


def resolve_configured_asgi_app_target(
    *,
    project_root: Path,
    config_source: str | None,
) -> str:
    app_config = load_required_app_config(
        project_root=project_root,
        config_source=config_source,
    )
    return _required_runserver_value(
        app_config.runserver.asgi_app,
        "[app.runserver].asgi_app",
    )


def load_required_app_config(
    *,
    project_root: Path,
    config_source: str | None,
) -> AppConfig:
    if config_source is not None:
        return load_app_config(
            project_root=project_root,
            config_path=Path(normalise_config_source(config_source)),
        )
    return load_app_config(project_root=project_root)


def _required_runserver_value(value: str | None, option_name: str) -> str:
    if value is not None and value.strip():
        return value.strip()
    raise ProjectToolConfigurationError(
        f"{option_name} must be configured in the selected app config file."
    )


def normalise_config_source(config_source: str) -> str:
    if config_source.strip():
        return config_source.strip()
    raise ProjectToolConfigurationError("--config must not be blank.")


__all__ = [
    "ConfiguredAppStartup",
    "load_required_app_config",
    "normalise_config_source",
    "resolve_configured_app_startup",
    "resolve_configured_asgi_app_target",
]
