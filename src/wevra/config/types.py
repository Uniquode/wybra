from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

EnvOverride = str | tuple[str, ...]
ConfigDiagnosticSeverity = Literal["error", "warning", "info"]


class ConfigError(RuntimeError):
    """Raised when configuration service operations fail."""


class ConfigSourceError(ConfigError):
    """Raised when a required configuration source cannot load."""


class ConfigDefinitionError(ConfigError):
    """Raised when a module config definition is invalid."""


@dataclass(frozen=True, slots=True)
class ConfigSourceMetadata:
    source: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class ConfigSourceLocation:
    file: Path | None = None
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigDiagnostic:
    source: ConfigSourceMetadata
    message: str
    severity: ConfigDiagnosticSeverity = "error"
    code: str | None = None
    location: ConfigSourceLocation | None = None


@dataclass(frozen=True, slots=True)
class ConfigSection:
    fields: frozenset[str] = frozenset()
    defaults: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __init__(
        self,
        *,
        fields: set[str] | frozenset[str] | tuple[str, ...] = (),
        defaults: Mapping[str, Any] | None = None,
        env: Mapping[str, EnvOverride] | None = None,
    ) -> None:
        declared_fields = frozenset(fields)
        default_values = dict(defaults or {})
        env_values = {
            field_name: _normalise_env_override(value)
            for field_name, value in (env or {}).items()
        }
        unknown_defaults = set(default_values) - declared_fields
        if unknown_defaults:
            raise ConfigDefinitionError(
                "Config defaults contain keys not declared in fields: "
                + ", ".join(sorted(unknown_defaults))
            )
        unknown_env = set(env_values) - declared_fields
        if unknown_env:
            raise ConfigDefinitionError(
                "Config env overrides contain keys not declared in fields: "
                + ", ".join(sorted(unknown_env))
            )
        object.__setattr__(self, "fields", declared_fields)
        object.__setattr__(self, "defaults", MappingProxyType(default_values))
        object.__setattr__(self, "env", MappingProxyType(env_values))


@dataclass(frozen=True, slots=True)
class ConfigDef:
    sections: Mapping[str, ConfigSection]

    def __init__(self, sections: Mapping[str, ConfigSection]) -> None:
        object.__setattr__(self, "sections", MappingProxyType(dict(sections)))


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """Loaded configuration with immutable mapping containers.

    Config values are intentionally preserved as raw source values. Nested
    mutable values are not coerced into different container types by this layer.
    """

    values: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[ConfigDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze_nested_mapping(self.values))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    def get_config(self, section: str) -> Mapping[str, Any] | None:
        return self.values.get(section)


@dataclass(frozen=True, slots=True)
class ConfigSourceResult:
    values: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    diagnostics: tuple[ConfigDiagnostic, ...] = ()


class ConfigSource(Protocol):
    @property
    def metadata(self) -> ConfigSourceMetadata: ...

    def load(self) -> ConfigSourceResult: ...


def merge_config_defs(definitions: tuple[ConfigDef, ...]) -> ConfigDef:
    sections: dict[str, ConfigSection] = {}
    for definition in definitions:
        for section_name, section in definition.sections.items():
            existing = sections.get(section_name)
            if existing is None:
                sections[section_name] = section
                continue

            merged_defaults = dict(existing.defaults)
            for field_name, value in section.defaults.items():
                if (
                    field_name in existing.defaults
                    and existing.defaults[field_name] != value
                ):
                    raise ConfigDefinitionError(
                        f"Conflicting default for field {section_name}.{field_name}."
                    )
                merged_defaults[field_name] = value

            merged_env = dict(existing.env)
            for field_name, value in section.env.items():
                if field_name in existing.env and existing.env[field_name] != value:
                    raise ConfigDefinitionError(
                        f"Conflicting env override for field "
                        f"{section_name}.{field_name}."
                    )
                merged_env[field_name] = value

            sections[section_name] = ConfigSection(
                fields=existing.fields | section.fields,
                defaults=merged_defaults,
                env=merged_env,
            )
    return ConfigDef(sections)


def _normalise_env_override(value: EnvOverride) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return value
    return (value,)


def _freeze_nested_mapping(
    values: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    """Freeze section mappings while preserving raw configured values.

    Only the top two mapping levels are frozen. Nested mutable values, such as
    lists or dicts stored inside sections, are not copied or frozen by this raw
    configuration layer and therefore remain shared and mutable.
    """

    return MappingProxyType(
        {
            section: MappingProxyType(dict(section_values))
            for section, section_values in values.items()
        }
    )
