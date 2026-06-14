from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Protocol, cast

from envex import Env

from wevra.config.types import ConfigDef, config_environment_names
from wevra.core.exceptions import ConfigurationError


def load_environment(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    read_dotenv: bool = True,
) -> Env:
    """Load process and local dotenv configuration through envex."""
    if environ is None:
        configured_environment: MutableMapping[str, str] = os.environ
    elif isinstance(environ, MutableMapping):
        configured_environment = cast(MutableMapping[str, str], environ)
    else:
        configured_environment = dict(environ)

    base_environment = (
        dict(configured_environment) if read_dotenv else configured_environment
    )
    try:
        return Env(
            environ=base_environment,
            readenv=read_dotenv,
            search_path=project_root or Path.cwd(),
            update=False,
        )
    except Exception as exc:
        raise ConfigurationError(
            "Environment loader failed while initialising envex "
            f"({type(exc).__name__})."
        ) from exc


class LoadedEnvironment(Protocol):
    def is_set(self, _var: str, /) -> bool: ...

    def get(self, _var: str, /) -> str | None: ...


class EnvironmentMapping(Mapping[str, str]):
    """Mapping adapter for envex values used by config-service env lookup."""

    def __init__(self, env: LoadedEnvironment) -> None:
        self._env = env

    def __getitem__(self, key: str) -> str:
        value = self._env.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._env.is_set(key)


def environment_mapping(
    env: LoadedEnvironment,
    config_defs: Iterable[ConfigDef],
    *,
    extra_names: Iterable[str] = (),
) -> dict[str, str]:
    """Return configured environment values relevant to config definitions."""
    names = set(extra_names)
    for config_def in config_defs:
        names.update(config_environment_names(config_def))
    return {
        name: value
        for name in names
        if env.is_set(name) and (value := env.get(name)) is not None
    }


__all__ = (
    "EnvironmentMapping",
    "LoadedEnvironment",
    "environment_mapping",
    "load_environment",
)
