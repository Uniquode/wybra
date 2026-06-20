from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from wybra.config import BaseSettings
from wybra.config.transforms import to_bool, to_raw_path
from wybra.config.types import ConfigDef
from wybra.template.config import module_config
from wybra.utils.paths import resolve_project_path


@dataclass(frozen=True, slots=True)
class TemplateSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = "app.templates"

    project_root: Path = Path.cwd()
    root: Path | None = None
    auto_reload: bool | None = None
    cache_size: int = 400
    request_context_enabled: bool = True

    @classmethod
    def load_settings(cls, config) -> TemplateSettings:  # type: ignore[override]
        app_values = cls.section_values(config, "app")
        project_root = to_raw_path(
            app_values.get("project_root", Path.cwd()),
            name="app.project_root",
        )
        return cls(project_root=project_root, **cls.settings_kwargs(config))

    def __post_init__(self) -> None:
        project_root = to_raw_path(self.project_root, name="app.project_root").resolve()
        root = _resolve_optional_path(self.root, project_root)
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "auto_reload", _optional_bool(self.auto_reload))
        object.__setattr__(self, "cache_size", _non_negative_int(self.cache_size))
        object.__setattr__(
            self,
            "request_context_enabled",
            to_bool(self.request_context_enabled),
        )


def _resolve_optional_path(value: Path | str | None, project_root: Path) -> Path | None:
    if value is None:
        return None
    return resolve_project_path(
        project_root,
        to_raw_path(value, name="app.templates.root"),
    )


def _optional_bool(value: bool | str | None) -> bool | None:
    if value is None:
        return None
    return to_bool(value)


def _non_negative_int(value: object) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    raise ValueError("app.templates.cache_size must be a non-negative integer.")


__all__ = ("TemplateSettings",)
