from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from wevra.config.types import (
    ConfigDiagnostic,
    ConfigSourceLocation,
    ConfigSourceMetadata,
    ConfigSourceResult,
)
from wevra.core.composition import AppConfig, CompositionError, load_app_config
from wevra.core.settings import EnvironmentSetting


class MappingConfigSource:
    def __init__(
        self,
        values: Mapping[str, Mapping[str, Any]],
        *,
        source: str = "mapping",
        required: bool = True,
    ) -> None:
        self._values = {
            section: dict(section_values) for section, section_values in values.items()
        }
        self._metadata = ConfigSourceMetadata(source=source, required=required)

    @property
    def metadata(self) -> ConfigSourceMetadata:
        return self._metadata

    def load(self) -> ConfigSourceResult:
        return ConfigSourceResult(values=self._values)


class EnvironmentConfigSource:
    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        env_settings: tuple[EnvironmentSetting, ...] = (),
        section: str = "environment",
        source: str = "environment",
        required: bool = True,
    ) -> None:
        self._environ = environ
        self._env_settings = env_settings
        self._section = section
        self._metadata = ConfigSourceMetadata(source=source, required=required)

    @property
    def metadata(self) -> ConfigSourceMetadata:
        return self._metadata

    def load(self) -> ConfigSourceResult:
        try:
            return ConfigSourceResult(values={self._section: self._values()})
        except ValueError as exc:
            return ConfigSourceResult(
                diagnostics=(
                    ConfigDiagnostic(
                        source=self.metadata,
                        message=str(exc),
                        code="environment_config_error",
                    ),
                )
            )

    def _values(self) -> dict[str, Any]:
        if not self._env_settings:
            return dict(self._environ)
        values: dict[str, Any] = {}
        for setting in self._env_settings:
            if setting.name not in self._environ:
                continue
            try:
                values[setting.field_name] = _parse_env_value(
                    self._environ[setting.name],
                    setting,
                )
            except ValueError as exc:
                raise ValueError(
                    f"{setting.name} for {setting.field_name}: {exc}"
                ) from exc
        return values


class AppConfigSource:
    def __init__(
        self,
        app_config: AppConfig,
        *,
        source: str = "app-config",
        required: bool = True,
    ) -> None:
        self._app_config = app_config
        self._metadata = ConfigSourceMetadata(source=source, required=required)

    @property
    def metadata(self) -> ConfigSourceMetadata:
        return self._metadata

    def load(self) -> ConfigSourceResult:
        return ConfigSourceResult(values=_app_config_sections(self._app_config))


class FileConfigSource:
    def __init__(
        self,
        config_path: Path,
        *,
        project_root: Path | None = None,
        source: str = "file",
        required: bool = True,
        loader: Callable[..., AppConfig] = load_app_config,
    ) -> None:
        self._config_path = config_path
        self._project_root = project_root
        self._loader = loader
        self._metadata = ConfigSourceMetadata(source=source, required=required)

    @property
    def metadata(self) -> ConfigSourceMetadata:
        return self._metadata

    def load(self) -> ConfigSourceResult:
        try:
            app_config = self._loader(
                project_root=self._project_root,
                config_path=self._config_path,
            )
        except CompositionError as exc:
            return ConfigSourceResult(
                diagnostics=(
                    ConfigDiagnostic(
                        source=self.metadata,
                        message=str(exc),
                        code="file_config_error",
                        location=ConfigSourceLocation(file=self._config_path),
                    ),
                )
            )

        return ConfigSourceResult(values=_app_config_sections(app_config))


def _parse_env_value(value: str, setting: EnvironmentSetting) -> Any:
    if not value.strip():
        raise ValueError(f"{setting.name} must not be blank.")
    match setting.value_type:
        case "path":
            return Path(value)
        case "bool":
            return _parse_bool(value, setting.name)
        case "int":
            try:
                return int(value)
            except ValueError as exc:
                raise ValueError(f"{setting.name} must be an integer value.") from exc
        case _:
            return value


def _parse_bool(value: str, name: str) -> bool:
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _app_config_sections(app_config: AppConfig) -> Mapping[str, Mapping[str, Any]]:
    values: dict[str, dict[str, Any]] = {
        section: dict(section_values)
        for section, section_values in app_config.raw_config.items()
    }
    values.setdefault("app", {}).update(
        {
            "config_path": app_config.config_path,
            "project_root": app_config.project_root,
            "modules": app_config.modules,
            "database_url": app_config.database_url,
            "deployment_environment": app_config.deployment_environment,
        }
    )
    values.setdefault("app.routes", {}).update({"prefixes": app_config.routes.prefixes})
    values.setdefault("app.static", {}).update(
        {
            "url_path": app_config.static.url_path,
            "root": app_config.static.root,
            "export_root": app_config.static.export_root,
            "serve": app_config.static.serve,
        }
    )
    values.setdefault("app.templates", {}).update(
        {
            "auto_reload": app_config.templates.auto_reload,
            "cache_size": app_config.templates.cache_size,
            "root": app_config.templates.root,
        }
    )
    if app_config.auth:
        values.setdefault("auth", {}).update(dict(app_config.auth))
    return values
