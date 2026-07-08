from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, ClassVar, Self

from envex import Env

from wybra.config.service import ConfigService
from wybra.config.sources import AppConfigSource, FileConfigSource
from wybra.config.types import (
    ConfigDef,
    ConfigDefinitionError,
    ConfigSource,
    ConfigSourceError,
)
from wybra.core.composition import APP_CONFIG_ENV, AppConfig
from wybra.core.diagnostics import wrapped_error
from wybra.core.environment import load_environment
from wybra.core.exceptions import ConfigurationError
from wybra.core.settings import (
    SettingsLoadError,
    load_composition_config_from_environment,
)
from wybra.tools.project import runtime_project_root

SettingsConfigSource = str | AppConfig | ConfigSource | None


class BaseSettings:
    module_config: ClassVar[ConfigDef]
    config_section: ClassVar[str | None] = None

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Any],
    ) -> Self:
        return cls(**cls.settings_kwargs(config))

    @classmethod
    def settings_kwargs(
        cls,
        config: ConfigService | Mapping[str, Any],
        section_name: str | None = None,
    ) -> dict[str, Any]:
        """Return constructor kwargs for a section owned by this settings class.

        A plain mapping is treated as a bare section only when reading the
        class's default section and the mapping does not contain that section
        name. Explicit section reads and sectioned mappings use section_values().
        """
        explicit_section = section_name is not None
        section_name = section_name or _settings_config_section(cls)
        sections = _settings_config_sections(cls)
        if section_name not in sections:
            raise ConfigDefinitionError(
                f"{cls.__name__}.settings_kwargs() requires {cls.__name__}."
                f"module_config to declare section {section_name!r}. Use "
                "section_values() for cross-section reads."
            )
        section = sections[section_name]
        if (
            not explicit_section
            and not isinstance(config, ConfigService)
            and isinstance(config, Mapping)
            and section_name not in config
        ):
            values = dict(config)
            _transform_settings_values(section_name, section.field_map, values)
        else:
            values = cls.section_values(config, section_name)
        return _settings_kwargs(cls, values, section.field_names)

    @classmethod
    def section_values(
        cls,
        config: ConfigService | Mapping[str, Any],
        section_name: str,
    ) -> dict[str, Any]:
        """Return a config section for settings-class overrides.

        Unlike settings_kwargs(), this helper intentionally allows cross-section
        reads so overrides can combine module config with app-level inputs.
        """
        if isinstance(config, ConfigService):
            return dict(config.get_config(section_name) or {})
        if not isinstance(config, Mapping):
            raise ConfigSourceError(
                f"Config source for section {section_name!r} must be a mapping "
                f"or ConfigService; got {type(config).__name__}."
            )
        configured_section = config.get(section_name)
        if configured_section is None:
            return {}
        if not isinstance(configured_section, Mapping):
            raise ConfigSourceError(
                f"Config section {section_name!r} must be a mapping; got "
                f"{type(configured_section).__name__}."
            )
        values = dict(configured_section)
        section = _settings_config_sections(cls).get(section_name)
        if section is not None:
            _transform_settings_values(section_name, section.field_map, values)
        return values


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
            environ=env,
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
    sections = _settings_config_sections(settings_type)
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


def _settings_config_sections(
    settings_type: type[BaseSettings],
) -> Mapping[str, Any]:
    try:
        module_config = settings_type.module_config
        sections = module_config.sections
    except AttributeError as exc:
        module_config = getattr(settings_type, "module_config", None)
        if module_config is not None:
            raise ConfigDefinitionError(
                f"{settings_type.__name__}.module_config must be a ConfigDef, "
                f"not {type(module_config).__name__!r}."
            ) from exc
        raise ConfigDefinitionError(
            f"{settings_type.__name__}.module_config must be a ConfigDef."
        ) from exc
    if not isinstance(module_config, ConfigDef):
        raise ConfigDefinitionError(
            f"{settings_type.__name__}.module_config must be a ConfigDef, "
            f"not {type(module_config).__name__!r}."
        )
    return sections


def _transform_settings_values(
    section_name: str,
    fields: Mapping[str, Any],
    values: dict[str, Any],
) -> None:
    """Apply field transforms.

    Transforms should raise TypeError or ValueError for invalid config input so
    the error can be reported with section and field context.
    """
    for field_name, field_def in fields.items():
        if field_def.transform is None or field_name not in values:
            continue
        try:
            values[field_name] = field_def.transform(values[field_name])
        except (TypeError, ValueError) as exc:
            raise ConfigSourceError(
                f"Config value {section_name}.{field_name} is invalid "
                f"({exc.__class__.__name__})."
            ) from exc


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
