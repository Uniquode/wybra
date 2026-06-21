"""Static asset settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from wybra.assets.config import (
    DEFAULT_ASSET_ROOT,
    AssetExportMode,
    _export_mode_value,
    _path_value,
    _url_path_value,
    module_config,
)
from wybra.config import BaseSettings
from wybra.config.transforms import to_bool
from wybra.config.types import ConfigDef
from wybra.utils.paths import resolve_project_path


@dataclass(frozen=True, slots=True)
class AssetSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = "app.assets"

    project_root: Path = Path.cwd()
    url_path: str = "/static/"
    root: Path = DEFAULT_ASSET_ROOT
    export_mode: AssetExportMode = AssetExportMode.NORMAL
    serve: bool = True

    @classmethod
    def load_settings(cls, config) -> AssetSettings:  # type: ignore[override]
        app_values = cls.section_values(config, "app")
        project_root = _path_value(
            app_values.get("project_root", Path.cwd()),
            name="app.project_root",
        )
        return cls(
            project_root=project_root,
            **cls.settings_kwargs(config),
        )

    def __post_init__(self) -> None:
        project_root = _path_value(self.project_root, name="app.project_root").resolve()
        root = resolve_project_path(project_root, _path_value(self.root))
        if root is None:  # pragma: no cover - _path_value prevents this
            root = project_root / DEFAULT_ASSET_ROOT
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "url_path", _url_path_value(self.url_path))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "export_mode", _export_mode_value(self.export_mode))
        object.__setattr__(self, "serve", to_bool(self.serve))


__all__ = ("AssetSettings",)
