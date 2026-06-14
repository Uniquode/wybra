from __future__ import annotations

import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any


class ProjectToolConfigurationError(ValueError):
    """Raised when Wevra project tool metadata is missing or invalid."""


def runtime_project_root(start: Path | None = None) -> Path:
    root = (start or Path.cwd()).resolve()
    for candidate in (root, *root.parents):
        pyproject = _read_pyproject(candidate)
        if pyproject is None:
            continue
        if _has_wevra_tool_options(pyproject):
            return candidate

        workspace_project_root = _single_workspace_wevra_project(candidate, pyproject)
        if workspace_project_root is not None:
            return workspace_project_root

        return candidate

    return root


def wevra_tool_options(project_root: Path | None = None) -> dict[str, Any]:
    root = runtime_project_root() if project_root is None else project_root
    pyproject_path = root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectToolConfigurationError(
            f"Wevra project metadata could not be read from {pyproject_path}."
        ) from exc

    tool_options = pyproject.get("tool", {}).get("wevra", {})
    if not isinstance(tool_options, dict):
        raise ProjectToolConfigurationError(
            "[tool.wevra] must be a table in pyproject.toml."
        )

    return tool_options


def _read_pyproject(project_root: Path) -> dict[str, Any] | None:
    pyproject_path = project_root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except OSError:
        return None
    except tomllib.TOMLDecodeError as exc:
        raise ProjectToolConfigurationError(
            f"Project metadata is invalid TOML: {pyproject_path}."
        ) from exc

    return pyproject if isinstance(pyproject, dict) else None


def _has_wevra_tool_options(pyproject: dict[str, Any]) -> bool:
    tool_config = pyproject.get("tool", {})
    return isinstance(tool_config, dict) and isinstance(
        tool_config.get("wevra"),
        dict,
    )


def _single_workspace_wevra_project(
    project_root: Path,
    pyproject: dict[str, Any],
) -> Path | None:
    tool_config = pyproject.get("tool", {})
    if not isinstance(tool_config, dict):
        return None
    uv_config = tool_config.get("uv", {})
    if not isinstance(uv_config, dict):
        return None
    workspace_config = uv_config.get("workspace", {})
    if not isinstance(workspace_config, dict):
        return None

    members = workspace_config.get("members", ())
    if not isinstance(members, list):
        return None

    wevra_projects = tuple(
        member_root
        for member_root in (
            (project_root / member).resolve()
            for member in members
            if isinstance(member, str)
        )
        if (member_pyproject := _read_pyproject(member_root)) is not None
        and _has_wevra_tool_options(member_pyproject)
    )
    if len(wevra_projects) > 1:
        project_list = ", ".join(path.as_posix() for path in wevra_projects)
        raise ProjectToolConfigurationError(
            "Workspace contains multiple projects with [tool.wevra]; run the "
            f"command from one project explicitly. Candidates: {project_list}."
        )

    return wevra_projects[0] if wevra_projects else None


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
