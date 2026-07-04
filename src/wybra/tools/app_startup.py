from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from wybra.core.composition import (
    AppConfig,
    load_app_config,
)
from wybra.tools.project import ProjectToolConfigurationError

CONFIG_SOURCE_CONTEXT_KEY = "config_source"
CONFIG_SOURCE_HELP = "App config file for this invocation."
CONFIG_SOURCE_OPTION = "--config"
type ConfigSourceErrorFactory = Callable[[str], Exception]


@dataclass(frozen=True, slots=True)
class ConfiguredAppStartup:
    app_target: str
    reload_env_var: str
    app_config: AppConfig


@dataclass(frozen=True, slots=True)
class ConfiguredAsgiAppTarget:
    app_target: str
    app_config: AppConfig


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
        app_config=app_config,
    )


def resolve_configured_asgi_app_target(
    *,
    project_root: Path,
    config_source: str | None,
) -> str:
    return resolve_configured_asgi_app(
        project_root=project_root,
        config_source=config_source,
    ).app_target


def resolve_configured_asgi_app(
    *,
    project_root: Path,
    config_source: str | None,
) -> ConfiguredAsgiAppTarget:
    app_config = load_required_app_config(
        project_root=project_root,
        config_source=config_source,
    )
    return ConfiguredAsgiAppTarget(
        app_target=_required_runserver_value(
            app_config.runserver.asgi_app,
            "[app.runserver].asgi_app",
        ),
        app_config=app_config,
    )


def load_required_app_config(
    *,
    project_root: Path,
    config_source: str | None,
) -> AppConfig:
    if config_source is not None:
        return load_app_config(
            project_root=project_root,
            config_path=Path(normalise_cli_config_source(config_source)),
        )
    return load_app_config(project_root=project_root)


def _required_runserver_value(value: str | None, option_name: str) -> str:
    if value is not None and value.strip():
        return value.strip()
    raise ProjectToolConfigurationError(
        f"{option_name} must be configured in the selected app config file."
    )


def normalise_config_source(config_source: str) -> str:
    normalised = config_source.strip()
    if normalised:
        return normalised
    raise ProjectToolConfigurationError("Configuration source must not be blank.")


def normalise_cli_config_source(config_source: str) -> str:
    try:
        return normalise_config_source(config_source)
    except ProjectToolConfigurationError as exc:
        raise ProjectToolConfigurationError(
            f"{CONFIG_SOURCE_OPTION} must not be blank."
        ) from exc


def config_source_from_click_context(
    ctx: click.Context,
    *,
    error_factory: ConfigSourceErrorFactory,
    invalid_context_message: str | None = None,
    invalid_type_message: Callable[[type[object]], str],
) -> str | None:
    root_context = ctx.find_root()
    obj = root_context.obj
    if not isinstance(obj, dict):
        if invalid_context_message is None:
            return None
        raise error_factory(invalid_context_message)

    value = obj.get(CONFIG_SOURCE_CONTEXT_KEY)
    if value is None:
        return None
    if not isinstance(value, str):
        raise error_factory(invalid_type_message(type(value)))
    try:
        return normalise_cli_config_source(value)
    except ProjectToolConfigurationError as exc:
        raise error_factory(str(exc)) from exc


__all__ = [
    "CONFIG_SOURCE_CONTEXT_KEY",
    "CONFIG_SOURCE_HELP",
    "CONFIG_SOURCE_OPTION",
    "ConfiguredAsgiAppTarget",
    "ConfiguredAppStartup",
    "config_source_from_click_context",
    "load_required_app_config",
    "normalise_cli_config_source",
    "normalise_config_source",
    "resolve_configured_asgi_app",
    "resolve_configured_app_startup",
    "resolve_configured_asgi_app_target",
]
