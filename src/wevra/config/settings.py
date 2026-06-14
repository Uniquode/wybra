from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, ClassVar, Self

from envex import Env

from wevra.config.service import ConfigService
from wevra.config.sources import AppConfigSource, FileConfigSource
from wevra.config.types import (
    ConfigDef,
    ConfigDefinitionError,
    ConfigSource,
    ConfigSourceError,
)
from wevra.core.composition import APP_CONFIG_ENV, AppConfig
from wevra.core.diagnostics import wrapped_error
from wevra.core.environment import environment_mapping, load_environment
from wevra.core.exceptions import ConfigurationError
from wevra.core.settings import (
    SettingsLoadError,
    load_composition_config_from_environment,
)
from wevra.tools.project import runtime_project_root

SettingsConfigSource = str | AppConfig | ConfigSource | None


class BaseSettings:
    module_config: ClassVar[ConfigDef]
    config_section: ClassVar[str | None] = None

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Any],
    ) -> Self:
        section_name = _settings_config_section(cls)
        if isinstance(config, ConfigService):
            values = dict(config.get_config(section_name) or {})
        else:
            values = dict(config)
        declared_fields = cls.module_config.sections[section_name].field_names
        return cls(**_settings_kwargs(cls, values, declared_fields))


def load_configured_settings[SettingsT: BaseSettings](
    settings_type: type[SettingsT],
    *,
    config_source: SettingsConfigSource = None,
) -> SettingsT:
    resolved_project_root = runtime_project_root().resolve()
    config_def = settings_type.module_config
    try:
        env = load_environment(
            project_root=resolved_project_root,
        )
        source = _settings_config_source(
            config_source,
            env,
            project_root=resolved_project_root,
        )
        config = ConfigService(
            [source],
            config_defs=(config_def,),
            environ=environment_mapping(
                env,
                (config_def,),
                extra_names=(APP_CONFIG_ENV,),
            ),
            discover_module_config=False,
        )
        return settings_type.load_settings(config)
    except (SettingsLoadError, ConfigSourceError) as exc:
        raise wrapped_error(ConfigurationError, exc) from exc


def _settings_config_source(
    config_source: SettingsConfigSource,
    env: Env,
    *,
    project_root: Path,
) -> ConfigSource:
    if config_source is None:
        app_config = load_composition_config_from_environment(
            env,
            project_root=project_root,
            require_app_config=True,
        )
        if app_config is None:  # pragma: no cover - require_app_config prevents this
            raise SettingsLoadError(
                "Application config file could not be resolved; run from the "
                f"app project or set {APP_CONFIG_ENV}."
            )
        return AppConfigSource(app_config)
    if isinstance(config_source, AppConfig):
        return AppConfigSource(config_source)
    if isinstance(config_source, str):
        return FileConfigSource(Path(config_source), project_root=project_root)
    return config_source


def _settings_kwargs[SettingsT: BaseSettings](
    settings_type: type[SettingsT],
    values: Mapping[str, Any],
    declared_fields: frozenset[str],
) -> dict[str, Any]:
    allowed = _settings_field_names(settings_type)
    return {
        key: value
        for key, value in values.items()
        if key in declared_fields and key in allowed
    }


def _settings_config_section[SettingsT: BaseSettings](
    settings_type: type[SettingsT],
) -> str:
    config_section = settings_type.config_section
    sections = settings_type.module_config.sections
    if config_section is not None:
        if config_section not in sections:
            raise ConfigDefinitionError(
                f"{settings_type.__name__}.config_section must name a section "
                "declared by module_config."
            )
        return config_section

    if len(sections) == 1:
        return next(iter(sections))

    raise ConfigDefinitionError(
        f"{settings_type.__name__}.config_section must be set when module_config "
        "declares multiple sections."
    )


def _settings_field_names(settings_type: type[object]) -> frozenset[str]:
    if is_dataclass(settings_type):
        return frozenset(field.name for field in fields(settings_type) if field.init)

    constructor = signature(settings_type)
    return frozenset(
        name
        for name, parameter in constructor.parameters.items()
        if parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
    )


__all__ = ("BaseSettings", "SettingsConfigSource", "load_configured_settings")
