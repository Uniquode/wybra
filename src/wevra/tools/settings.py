from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wevra.config import (
    AppConfigSource,
    ConfigDef,
    ConfigDefinitionError,
    ConfigService,
    ConfigSourceError,
    discover_module_config_defs,
)
from wevra.core.composition import APP_CONFIG_ENV, DEFAULT_APP_CONFIG, AppConfig
from wevra.core.config import RUNTIME_CONFIG_DEF
from wevra.core.diagnostics import wrapped_error
from wevra.core.environment import environment_mapping, load_environment
from wevra.core.exceptions import ConfigurationError
from wevra.core.settings import (
    SettingsLoadError,
    load_composition_config_from_environment,
)
from wevra.db.config import DEFAULT_ALEMBIC_CONFIG
from wevra.db.urls import resolve_database_url


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    project_root: Path
    app_config: AppConfig
    database_url: str | None = None
    template_root: Path | None = None
    static_root: Path | None = None
    migrations_root: Path | None = None
    alembic_config: Path = DEFAULT_ALEMBIC_CONFIG
    static_url_path: str = "/static/"
    template_auto_reload: bool | None = None
    template_cache_size: int = 400

    def __post_init__(self) -> None:
        project_root = self.project_root.resolve()
        object.__setattr__(self, "project_root", project_root)
        if self.database_url is not None:
            object.__setattr__(
                self,
                "database_url",
                resolve_database_url(self.database_url, project_root),
            )
        object.__setattr__(
            self,
            "template_root",
            _resolve_optional_path(self.template_root, project_root, "template_root"),
        )
        object.__setattr__(
            self,
            "static_root",
            _resolve_optional_path(self.static_root, project_root, "static_root"),
        )
        object.__setattr__(
            self,
            "migrations_root",
            _resolve_optional_path(
                self.migrations_root,
                project_root,
                "migrations_root",
            ),
        )
        object.__setattr__(
            self,
            "alembic_config",
            _resolve_path(
                self.alembic_config,
                project_root,
                DEFAULT_ALEMBIC_CONFIG,
                "alembic_config",
            ),
        )
        object.__setattr__(
            self,
            "static_url_path",
            _non_blank_string(self.static_url_path, "static_url_path"),
        )
        object.__setattr__(
            self,
            "template_auto_reload",
            _optional_bool(self.template_auto_reload, "template_auto_reload"),
        )
        object.__setattr__(
            self,
            "template_cache_size",
            _non_negative_int(self.template_cache_size, "template_cache_size"),
        )

    @property
    def modules(self) -> tuple[str, ...]:
        return self.app_config.modules

    @property
    def uses_filesystem_template_root(self) -> bool:
        return self.template_root is not None

    @property
    def uses_filesystem_static_root(self) -> bool:
        return self.static_root is not None


def load_project_settings(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    read_dotenv: bool = True,
) -> ProjectSettings:
    resolved_project_root = (project_root or Path.cwd()).resolve()
    try:
        env = load_environment(
            environ=environ,
            project_root=resolved_project_root,
            read_dotenv=read_dotenv,
        )
        app_config = load_composition_config_from_environment(
            env,
            project_root=resolved_project_root,
            app_config_env=APP_CONFIG_ENV,
            default_app_config=DEFAULT_APP_CONFIG,
            require_app_config=True,
        )
        if app_config is None:  # pragma: no cover - require_app_config prevents this
            raise ConfigurationError(
                "Application config file could not be resolved; run from the "
                f"app project or set {APP_CONFIG_ENV}."
            )
        config_defs = _project_config_defs(app_config)
        config = ConfigService(
            [AppConfigSource(app_config)],
            config_defs=config_defs,
            environ=environment_mapping(
                env,
                config_defs,
                extra_names=(APP_CONFIG_ENV,),
            ),
        )
        return ProjectSettings(**_project_settings_kwargs(config, app_config))
    except (SettingsLoadError, ConfigDefinitionError, ConfigSourceError) as exc:
        raise wrapped_error(ConfigurationError, exc) from exc


def _project_config_defs(app_config: AppConfig) -> tuple[ConfigDef, ...]:
    return (RUNTIME_CONFIG_DEF, *discover_module_config_defs(app_config.modules))


def _project_settings_kwargs(
    config: ConfigService,
    app_config: AppConfig,
) -> dict[str, Any]:
    app_values = dict(config.get_config("app") or {})
    static_values = dict(config.get_config("app.static") or {})
    template_values = dict(config.get_config("app.templates") or {})
    database_url = _configured_database_url(app_values, app_config.database_url)
    settings_kwargs: dict[str, Any] = {
        "project_root": app_config.project_root,
        "app_config": app_config,
        "static_url_path": static_values.get("url_path", app_config.static.url_path),
        "template_auto_reload": template_values.get(
            "auto_reload",
            app_config.templates.auto_reload,
        ),
        "template_cache_size": template_values.get(
            "cache_size",
            app_config.templates.cache_size,
        ),
    }
    if database_url is not None:
        settings_kwargs["database_url"] = database_url
    for field_name in (
        "alembic_config",
        "migrations_root",
    ):
        if field_name in app_values:
            settings_kwargs[field_name] = app_values[field_name]
    if "root" in static_values:
        settings_kwargs["static_root"] = static_values["root"]
    if "root" in template_values:
        settings_kwargs["template_root"] = template_values["root"]
    return settings_kwargs


def _configured_database_url(
    app_values: Mapping[str, Any],
    configured_database_url: str | None,
) -> str | None:
    database_url = app_values.get("database_url")
    if isinstance(database_url, str):
        if not database_url.strip():
            raise ConfigurationError("DATABASE_URL must not be blank.")
        return database_url
    return configured_database_url


def _resolve_optional_path(
    value: Path | str | None,
    project_root: Path,
    field_name: str,
) -> Path | None:
    if value is None:
        return None
    return _resolve_path(value, project_root, None, field_name)


def _resolve_path(
    value: Path | str | None,
    project_root: Path,
    default: Path | None,
    field_name: str,
) -> Path:
    path = value if value is not None else default
    if path is None:
        raise ConfigurationError(f"{field_name} must not be empty.")
    if isinstance(path, str):
        if not path.strip():
            raise ConfigurationError(f"{field_name} must not be empty.")
        path = Path(path)
    if not isinstance(path, Path):
        raise ConfigurationError(f"{field_name} must be a path.")
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _non_blank_string(value: object, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise ConfigurationError(f"{field_name} must not be blank.")


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"1", "true", "yes", "on"}:
            return True
        if normalised in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{field_name} must be a boolean.")


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"{field_name} must be a non-negative integer."
            ) from exc
        if parsed >= 0:
            return parsed
    raise ConfigurationError(f"{field_name} must be a non-negative integer.")


__all__ = (
    "ProjectSettings",
    "load_project_settings",
)
