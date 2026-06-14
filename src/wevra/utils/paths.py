from __future__ import annotations

from pathlib import Path


def resolve_project_path(project_root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


__all__ = ("resolve_project_path",)
