from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from envex import Env

from wybra.core.composition import (
    APP_CONFIG_ENV,
    DEFAULT_APP_CONFIG,
    AppConfig,
    CompositionError,
    load_app_config,
)
from wybra.core.diagnostics import wrapped_error

EnvironmentValueType = Literal["str", "path", "bool", "int"]


@dataclass(frozen=True, slots=True)
class EnvironmentSetting:
    name: str
    field_name: str
    value_type: EnvironmentValueType = "str"


class SettingsLoadError(ValueError):
    """Raised when reusable settings loading cannot build valid kwargs."""


class EnvironmentLoader(Protocol):
    def __call__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
        read_dotenv: bool = True,
    ) -> Env: ...


SettingsValueLoader = Callable[[Env], Mapping[str, Any] | None]
AppConfigValueLoader = Callable[[AppConfig, Env], Mapping[str, Any] | None]
SettingsT = TypeVar("SettingsT")


def load_composed_settings(  # noqa: UP047
    settings_factory: Callable[..., SettingsT],
    *,
    environment_loader: EnvironmentLoader,
    env_settings: Iterable[EnvironmentSetting],
    app_config_value_loaders: Iterable[AppConfigValueLoader] = (),
    extra_value_loaders: Iterable[SettingsValueLoader] = (),
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    read_dotenv: bool = True,
    app_config_env: str = APP_CONFIG_ENV,
    default_app_config: Path = DEFAULT_APP_CONFIG,
    require_app_config: bool = False,
) -> SettingsT:
    """Build application settings from envex values and optional app.toml.

    Applications keep their concrete settings class and policy validation, while
    this helper owns the reusable mechanics of reading typed environment
    settings, applying composition defaults, and invoking the settings factory.
    """
    env = environment_loader(
        environ=environ,
        project_root=project_root,
        read_dotenv=read_dotenv,
    )
    settings_kwargs: dict[str, Any] = {}
    if project_root is not None:
        settings_kwargs["project_root"] = project_root

    app_config = load_composition_config_from_environment(
        env,
        project_root=project_root,
        app_config_env=app_config_env,
        default_app_config=default_app_config,
        require_app_config=require_app_config,
    )
    if app_config is not None:
        settings_kwargs.setdefault("project_root", app_config.project_root)
        settings_kwargs["app_config"] = app_config
        settings_kwargs["static_url_path"] = app_config.static.url_path
        settings_kwargs["template_auto_reload"] = app_config.templates.auto_reload
        settings_kwargs["template_cache_size"] = app_config.templates.cache_size
        for value_loader in app_config_value_loaders:
            extra_values = value_loader(app_config, env)
            if extra_values:
                settings_kwargs.update(extra_values)

    settings_kwargs.update(values_from_env_settings(env, env_settings))
    for value_loader in extra_value_loaders:
        extra_values = value_loader(env)
        if extra_values:
            settings_kwargs.update(extra_values)

    return settings_factory(**settings_kwargs)


def load_composition_config_from_environment(
    env: Env,
    *,
    project_root: Path | None = None,
    app_config_env: str = APP_CONFIG_ENV,
    default_app_config: Path = DEFAULT_APP_CONFIG,
    require_app_config: bool = False,
) -> AppConfig | None:
    resolved_project_root = (project_root or Path.cwd()).resolve()
    if env.is_set(app_config_env):
        reject_blank_env_value(env, app_config_env)
        try:
            return load_app_config(
                project_root=resolved_project_root,
                environ={app_config_env: env.get(app_config_env) or ""},
            )
        except CompositionError as exc:
            raise wrapped_error(SettingsLoadError, exc) from exc

    default_config_path = resolved_project_root / default_app_config
    if not default_config_path.is_file():
        if require_app_config:
            raise SettingsLoadError(
                "Application config file could not be resolved; run from the "
                f"app project or set {app_config_env}."
            )
        return None

    try:
        return load_app_config(
            project_root=resolved_project_root,
            config_path=default_config_path,
        )
    except CompositionError as exc:
        raise wrapped_error(SettingsLoadError, exc) from exc


def values_from_env_settings(
    env: Env,
    env_settings: Iterable[EnvironmentSetting],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for env_setting in env_settings:
        if env_setting.value_type == "path":
            _set_env_path(env, values, env_setting.field_name, env_setting.name)
        elif env_setting.value_type == "bool":
            _set_env_bool(env, values, env_setting.field_name, env_setting.name)
        elif env_setting.value_type == "int":
            _set_env_int(env, values, env_setting.field_name, env_setting.name)
        else:
            _set_env_value(env, values, env_setting.field_name, env_setting.name)

    return values


def env_setting_is_set(env: Env, env_settings: Iterable[EnvironmentSetting]) -> bool:
    return any(env.is_set(env_setting.name) for env_setting in env_settings)


def reject_blank_env_value(env: Env, env_name: str) -> None:
    raw_value = env.get(env_name)
    if raw_value is None or not raw_value.strip():
        raise SettingsLoadError(f"{env_name} must not be blank.")


def _set_env_value(
    env: Env,
    values: dict[str, Any],
    setting_name: str,
    env_name: str,
    *,
    default: str | None = None,
) -> None:
    if env.is_set(env_name):
        reject_blank_env_value(env, env_name)
        values[setting_name] = env.get(env_name)
    elif default is not None:
        values[setting_name] = default


def _set_env_path(
    env: Env,
    values: dict[str, Any],
    setting_name: str,
    env_name: str,
) -> None:
    if env.is_set(env_name):
        reject_blank_env_value(env, env_name)
        values[setting_name] = Path(env.get(env_name))


def _set_env_bool(
    env: Env,
    values: dict[str, Any],
    setting_name: str,
    env_name: str,
) -> None:
    if env.is_set(env_name):
        reject_blank_env_value(env, env_name)
        try:
            values[setting_name] = env.bool(env_name)
        except ValueError as exc:
            raise SettingsLoadError(f"{env_name} must be a boolean value.") from exc


def _set_env_int(
    env: Env,
    values: dict[str, Any],
    setting_name: str,
    env_name: str,
) -> None:
    if env.is_set(env_name):
        reject_blank_env_value(env, env_name)
        try:
            values[setting_name] = env.int(env_name)
        except ValueError as exc:
            raise SettingsLoadError(f"{env_name} must be an integer value.") from exc
