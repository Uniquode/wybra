from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any


def runtime_project_root(start: Path | None = None) -> Path:
    root = (start or Path.cwd()).resolve()
    for candidate in (root, *root.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    return root


class ProjectToolConfigurationError(ValueError):
    """Raised when Wevra project tool adapter metadata is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ProjectToolRuntime:
    """Resolved project adapter hooks shared by Wevra command wrappers."""

    project_root: Path
    settings_loader: Callable[..., Any]
    configuration_error: type[Exception]


def wevra_tool_options(project_root: Path | None = None) -> dict[str, Any]:
    root = runtime_project_root() if project_root is None else project_root
    pyproject_path = root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except OSError as exc:
        raise ProjectToolConfigurationError(
            f"Wevra project metadata could not be read from {pyproject_path}."
        ) from exc

    tool_options = pyproject.get("tool", {}).get("wevra", {})
    if not isinstance(tool_options, dict):
        raise ProjectToolConfigurationError(
            "[tool.wevra] must be a table in pyproject.toml."
        )

    return tool_options


def wevra_tool_option(name: str, *, project_root: Path | None = None) -> str:
    value = wevra_tool_options(project_root).get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectToolConfigurationError(
            f"[tool.wevra].{name} must be configured as a non-blank string."
        )

    return value.strip()


def import_from_string(spec: str) -> Any:
    if not isinstance(spec, str):
        raise ProjectToolConfigurationError(
            f"Import spec must be configured as a string, got {type(spec).__name__!r}."
        )

    module_name, separator, attribute_name = spec.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ProjectToolConfigurationError(
            f"Import spec {spec!r} must use 'module:attribute' format."
        )

    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ProjectToolConfigurationError(
            f"Configured import module {module_name!r} could not be imported."
        ) from exc

    try:
        return getattr(module, attribute_name)
    except AttributeError as exc:
        raise ProjectToolConfigurationError(
            f"Configured import attribute {attribute_name!r} was not found on "
            f"{module_name!r}."
        ) from exc


def import_wevra_tool_option(name: str, *, project_root: Path | None = None) -> Any:
    return import_from_string(wevra_tool_option(name, project_root=project_root))


def import_wevra_tool_callable(
    name: str,
    *,
    project_root: Path | None = None,
) -> Callable[..., Any]:
    resolved = import_wevra_tool_option(name, project_root=project_root)
    if not callable(resolved):
        raise ProjectToolConfigurationError(
            f"[tool.wevra].{name} must resolve to a callable."
        )

    return resolved


def import_wevra_tool_exception_class(
    name: str,
    *,
    project_root: Path | None = None,
) -> type[Exception]:
    resolved = import_wevra_tool_option(name, project_root=project_root)
    if not isinstance(resolved, type) or not issubclass(resolved, Exception):
        raise ProjectToolConfigurationError(
            f"[tool.wevra].{name} must resolve to an exception class."
        )

    return resolved


def load_wevra_tool_runtime(
    *,
    project_root: Path | None = None,
) -> ProjectToolRuntime:
    root = runtime_project_root() if project_root is None else project_root
    return ProjectToolRuntime(
        project_root=root,
        settings_loader=import_wevra_tool_callable(
            "settings_loader",
            project_root=root,
        ),
        configuration_error=import_wevra_tool_exception_class(
            "configuration_error",
            project_root=root,
        ),
    )
