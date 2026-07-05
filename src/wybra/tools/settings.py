from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from wybra.config import (
    AppConfigSource,
    BaseSettings,
    ConfigDef,
    ConfigDefinitionError,
    ConfigService,
    ConfigSourceError,
    discover_module_config_defs,
)
from wybra.core.composition import (
    APP_CONFIG_ENV,
    AppConfig,
    resolve_project_root,
)
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.diagnostics import wrapped_error
from wybra.core.environment import environment_mapping, load_environment
from wybra.core.exceptions import ConfigurationError
from wybra.core.modules import CORE_MODULES
from wybra.core.runtime import (
    DEFAULT_DEPLOYMENT_ENVIRONMENT,
    DeploymentEnvironment,
    normalise_deployment_environment,
)
from wybra.core.settings import (
    SettingsLoadError,
    load_composition_config_from_environment,
)
from wybra.db.urls import resolve_database_url
from wybra.media.config import MEDIA_URL_MODES


@dataclass(frozen=True, slots=True)
class ProjectSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = RUNTIME_CONFIG_DEF
    config_section: ClassVar[str | None] = "app"

    project_root: Path
    app_config: AppConfig
    config: ConfigService
    database_url: str | None = None
    template_root: Path | None = None
    static_root: Path | None = None
    static_root_configured: bool = False
    migrations_root: Path | None = None
    static_url_path: str = "/static/"
    media_root: Path | None = None
    media_mount_path: str = "/media"
    media_serve: bool = False
    media_url_mode: str = "storage-key"
    template_auto_reload: bool | None = None
    template_cache_size: int = 400
    deployment_environment: DeploymentEnvironment = DEFAULT_DEPLOYMENT_ENVIRONMENT

    @classmethod
    def load_settings(
        cls,
        config: ConfigService,
        *,
        app_config: AppConfig,
    ) -> ProjectSettings:  # ty: ignore[invalid-method-override]
        return cls(**_project_settings_kwargs(config, app_config))

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
            "media_root",
            _resolve_optional_path(self.media_root, project_root, "media_root"),
        )
        object.__setattr__(
            self,
            "static_url_path",
            _non_blank_string(self.static_url_path, "static_url_path"),
        )
        object.__setattr__(
            self,
            "media_mount_path",
            _normalise_mount_path(self.media_mount_path, "media_mount_path"),
        )
        object.__setattr__(
            self,
            "media_serve",
            _required_bool(self.media_serve, "media_serve"),
        )
        object.__setattr__(
            self,
            "media_url_mode",
            _media_url_mode(self.media_url_mode),
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
        return self.static_root_configured and self.static_root is not None


def load_project_settings(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    read_dotenv: bool = True,
) -> ProjectSettings:
    resolved_project_root = resolve_project_root(project_root, environ)
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
            require_app_config=True,
        )
        if app_config is None:  # pragma: no cover - require_app_config prevents this
            raise ConfigurationError(
                "Application config file could not be resolved; pass --config or set "
                f"{APP_CONFIG_ENV}."
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
        return ProjectSettings.load_settings(config, app_config=app_config)
    except (SettingsLoadError, ConfigDefinitionError, ConfigSourceError) as exc:
        raise wrapped_error(ConfigurationError, exc) from exc


def _project_config_defs(app_config: AppConfig) -> tuple[ConfigDef, ...]:
    return (
        RUNTIME_CONFIG_DEF,
        *discover_module_config_defs(CORE_MODULES),
        *discover_module_config_defs(app_config.modules),
    )


def _project_settings_kwargs(
    config: ConfigService,
    app_config: AppConfig,
) -> dict[str, Any]:
    app_values = ProjectSettings.section_values(config, "app")
    static_values = ProjectSettings.section_values(config, "app.assets")
    template_values = ProjectSettings.section_values(config, "app.templates")
    media_values = ProjectSettings.section_values(config, "wybra.media")
    database_url = _configured_database_url(app_values, app_config.database_url)
    settings_kwargs: dict[str, Any] = {
        "project_root": app_config.project_root,
        "app_config": app_config,
        "config": config,
        "static_url_path": static_values.get("url_path", app_config.assets.url_path),
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
    settings_kwargs["deployment_environment"] = _deployment_environment(
        app_values,
        app_config,
    )
    for field_name in ("migrations_root",):
        if field_name in app_values:
            settings_kwargs[field_name] = app_values[field_name]
    if "root" in template_values:
        settings_kwargs["template_root"] = template_values["root"]
    if "root" in media_values:
        settings_kwargs["media_root"] = media_values["root"]
    if "mount_path" in media_values:
        settings_kwargs["media_mount_path"] = media_values["mount_path"]
    if "serve" in media_values:
        settings_kwargs["media_serve"] = media_values["serve"]
    if "url_mode" in media_values:
        settings_kwargs["media_url_mode"] = media_values["url_mode"]
    return settings_kwargs


def _deployment_environment(
    app_values: Mapping[str, Any],
    app_config: AppConfig,
) -> DeploymentEnvironment:
    value = app_values.get("deployment_environment")
    if value is not None:
        return normalise_deployment_environment(value)
    if app_config.deployment_environment is not None:
        return normalise_deployment_environment(app_config.deployment_environment)
    return DEFAULT_DEPLOYMENT_ENVIRONMENT


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


def _required_bool(value: object, field_name: str) -> bool:
    parsed = _optional_bool(value, field_name)
    if parsed is None:
        raise ConfigurationError(f"{field_name} must be a boolean.")
    return parsed


def _normalise_mount_path(value: object, field_name: str) -> str:
    path = _non_blank_string(value, field_name)
    return f"/{path.strip('/')}"


def _media_url_mode(value: object) -> str:
    if isinstance(value, str) and value.strip() in MEDIA_URL_MODES:
        return value.strip()
    allowed = ", ".join(sorted(MEDIA_URL_MODES))
    raise ConfigurationError(f"media_url_mode must be one of: {allowed}.")


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
