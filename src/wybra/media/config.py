from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from wybra.config import BaseSettings, ConfigDef, ConfigField, ConfigGroup, to_bool

DEFAULT_MEDIA_MOUNT_PATH: Final = "/media"
DEFAULT_MEDIA_ROOT: Final = Path("media")
DEFAULT_MEDIA_URL_MODE: Final = "storage-key"
ENV_MEDIA_MOUNT_PATH: Final = "MEDIA_MOUNT_PATH"
ENV_MEDIA_ROOT: Final = "MEDIA_ROOT"
ENV_MEDIA_SERVE: Final = "MEDIA_SERVE"
ENV_MEDIA_URL_MODE: Final = "MEDIA_URL_MODE"
MEDIA_URL_MODES: Final = frozenset({"storage-key", "id"})


def _path_value(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise ValueError("Path value must be a non-blank path.")


def _url_mode_value(value: object) -> str:
    if isinstance(value, str) and value.strip() in MEDIA_URL_MODES:
        return value.strip()
    allowed = ", ".join(sorted(MEDIA_URL_MODES))
    raise ValueError(f"Media URL mode must be one of: {allowed}.")


def _mount_path_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Media mount path must be a non-blank string.")
    return f"/{value.strip('/')}"


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("Media serve must be a boolean value.")


module_config: Final = ConfigDef(
    {
        "wybra.media": ConfigGroup(
            fields=(
                ConfigField(
                    name="root", default=DEFAULT_MEDIA_ROOT, env=ENV_MEDIA_ROOT
                ),
                ConfigField(
                    name="mount_path",
                    default=DEFAULT_MEDIA_MOUNT_PATH,
                    env=ENV_MEDIA_MOUNT_PATH,
                ),
                ConfigField(
                    name="serve",
                    default=True,
                    env=ENV_MEDIA_SERVE,
                    transform=to_bool,
                ),
                ConfigField(
                    name="url_mode",
                    default=DEFAULT_MEDIA_URL_MODE,
                    env=ENV_MEDIA_URL_MODE,
                    transform=_url_mode_value,
                ),
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class MediaSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config

    project_root: Path = Path.cwd()
    root: Path = DEFAULT_MEDIA_ROOT
    mount_path: str = DEFAULT_MEDIA_MOUNT_PATH
    serve: bool = True
    url_mode: str = DEFAULT_MEDIA_URL_MODE

    @classmethod
    def load_settings(cls, config) -> MediaSettings:  # type: ignore[override]
        app_config = cls.section_values(config, "app")
        project_root = _path_value(app_config.get("project_root", Path.cwd())).resolve()
        return cls(project_root=project_root, **cls.settings_kwargs(config))

    def __post_init__(self) -> None:
        project_root = _path_value(self.project_root).resolve()
        root = _path_value(self.root)
        if not root.is_absolute():
            root = project_root / root

        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "root", root.resolve())
        object.__setattr__(self, "mount_path", _mount_path_value(self.mount_path))
        object.__setattr__(self, "serve", _bool_value(self.serve))
        object.__setattr__(self, "url_mode", _url_mode_value(self.url_mode))


__all__ = (
    "DEFAULT_MEDIA_MOUNT_PATH",
    "DEFAULT_MEDIA_ROOT",
    "DEFAULT_MEDIA_URL_MODE",
    "ENV_MEDIA_MOUNT_PATH",
    "ENV_MEDIA_ROOT",
    "ENV_MEDIA_SERVE",
    "ENV_MEDIA_URL_MODE",
    "MEDIA_URL_MODES",
    "MediaSettings",
    "module_config",
)
