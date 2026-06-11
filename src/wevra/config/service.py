from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import import_module
from typing import Any

from wevra.config.types import (
    ConfigDef,
    ConfigDefinitionError,
    ConfigDiagnostic,
    ConfigSource,
    ConfigSourceError,
    ConfigSourceMetadata,
    ConfigSourceResult,
    LoadedConfig,
    merge_config_defs,
)

APP_SECTION = "app"
APP_MODULES_KEY = "modules"
MODULE_CONFIG_ATTRIBUTE = "module_config"


class ConfigService:
    def __init__(
        self,
        sources: Iterable[ConfigSource] = (),
        *,
        config_defs: Iterable[ConfigDef] = (),
        environ: Mapping[str, str] | None = None,
        discover_module_config: bool = True,
    ) -> None:
        self._sources = tuple(sources)
        self._config_defs = tuple(config_defs)
        self._environ = environ
        self._discover_module_config = discover_module_config
        self._config = self._load_sources()

    @property
    def config(self) -> LoadedConfig:
        return self._config

    @property
    def diagnostics(self) -> tuple[ConfigDiagnostic, ...]:
        return self._config.diagnostics

    def get_config(self, section: str) -> Mapping[str, Any] | None:
        return self._config.get_config(section)

    def _load_sources(self) -> LoadedConfig:
        source_values: dict[str, dict[str, Any]] = {}
        value_sources: dict[str, str] = {}
        diagnostics: list[ConfigDiagnostic] = []

        for source in self._sources:
            try:
                result = source.load()
            except ConfigSourceError as exc:
                message = _source_error_message(source.metadata, str(exc))
                if source.metadata.required:
                    raise ConfigSourceError(message) from exc
                diagnostics.append(
                    ConfigDiagnostic(
                        source=source.metadata,
                        message=message,
                        code="source_load_error",
                    )
                )
                continue

            diagnostics.extend(result.diagnostics)
            if _has_error_diagnostic(result):
                message = _first_error_message(result) or "Configuration source failed."
                if source.metadata.required:
                    raise ConfigSourceError(
                        _source_error_message(source.metadata, message)
                    )
                continue

            _merge_values(source_values, value_sources, result.values, source.metadata)

        config_defs = self._resolved_config_defs(source_values)
        values, sources = _apply_config_defs(
            config_defs,
            source_values,
            value_sources,
            self._environ,
        )
        return LoadedConfig(
            values=values,
            sources=sources,
            diagnostics=tuple(diagnostics),
        )

    def _resolved_config_defs(
        self,
        source_values: Mapping[str, Mapping[str, Any]],
    ) -> tuple[ConfigDef, ...]:
        if not self._discover_module_config:
            return self._config_defs
        return (
            *self._config_defs,
            *discover_module_config_defs(_bootstrap_modules(source_values)),
        )


def discover_module_config_defs(module_names: Iterable[str]) -> tuple[ConfigDef, ...]:
    definitions: list[ConfigDef] = []
    for module_name in module_names:
        module = import_module(module_name)
        module_config = getattr(module, MODULE_CONFIG_ATTRIBUTE, None)
        if module_config is None:
            continue
        if not isinstance(module_config, ConfigDef):
            raise ConfigDefinitionError(
                f"{module_name}.{MODULE_CONFIG_ATTRIBUTE} must be a ConfigDef."
            )
        definitions.append(module_config)
    return tuple(definitions)


def _bootstrap_modules(
    values: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    app_values = values.get(APP_SECTION, {})
    modules = app_values.get(APP_MODULES_KEY, ())
    if modules is None:
        return ()
    if isinstance(modules, str):
        raise ConfigDefinitionError(
            "[app].modules must be a list or tuple of module names."
        )
    if isinstance(modules, (tuple, list)) and all(
        isinstance(module, str) for module in modules
    ):
        return tuple(modules)
    raise ConfigDefinitionError(
        "[app].modules must be a list or tuple of module names."
    )


def _apply_config_defs(
    definitions: tuple[ConfigDef, ...],
    source_values: Mapping[str, Mapping[str, Any]],
    source_index: Mapping[str, str],
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    merged_def = merge_config_defs(definitions)
    values: dict[str, dict[str, Any]] = {
        section: dict(section_values)
        for section, section_values in _default_values(merged_def).items()
    }
    sources: dict[str, str] = {
        f"{section}.{key}": "default"
        for section, section_values in values.items()
        for key in section_values
    }

    _merge_indexed_values(values, sources, source_values, source_index)
    if environ is not None:
        _merge_values(
            values,
            sources,
            _env_values(merged_def, environ),
            ConfigSourceMetadata(source="environment"),
        )
    return values, sources


def _default_values(definition: ConfigDef) -> dict[str, dict[str, Any]]:
    return {
        section_name: dict(section.defaults)
        for section_name, section in definition.sections.items()
        if section.defaults
    }


def _env_values(
    definition: ConfigDef,
    environ: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for section_name, section in definition.sections.items():
        for field_name, env_names in section.env.items():
            env_name = next((name for name in env_names if name in environ), None)
            if env_name is not None:
                values.setdefault(section_name, {})[field_name] = environ[env_name]
    return values


def _merge_values(
    target: dict[str, dict[str, Any]],
    source_index: dict[str, str],
    values: Mapping[str, Mapping[str, Any]],
    metadata: ConfigSourceMetadata,
) -> None:
    for section, section_values in values.items():
        target_section = target.setdefault(section, {})
        target_section.update(section_values)
        for key in section_values:
            source_index[f"{section}.{key}"] = metadata.source


def _merge_indexed_values(
    target: dict[str, dict[str, Any]],
    target_index: dict[str, str],
    values: Mapping[str, Mapping[str, Any]],
    source_index: Mapping[str, str],
    default_source: str = "source",
) -> None:
    for section, section_values in values.items():
        target_section = target.setdefault(section, {})
        target_section.update(section_values)
        for key in section_values:
            index_key = f"{section}.{key}"
            target_index[index_key] = source_index.get(index_key, default_source)


def _has_error_diagnostic(result: ConfigSourceResult) -> bool:
    return any(diagnostic.severity == "error" for diagnostic in result.diagnostics)


def _first_error_message(result: ConfigSourceResult) -> str | None:
    return next(
        (
            diagnostic.message
            for diagnostic in result.diagnostics
            if diagnostic.severity == "error"
        ),
        None,
    )


def _source_error_message(metadata: ConfigSourceMetadata, message: str) -> str:
    return f"{metadata.source}: {message}" if message else metadata.source
