from __future__ import annotations

from pathlib import Path

from wybra.utils.paths import resolve_project_path


def to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"true", "1", "yes", "on"}:
            return True
        if normalised in {"false", "0", "no", "off"}:
            return False

    raise ValueError("must be a boolean value.")


def to_path(value: object, *, root: Path | None = None) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise ValueError("must be a path value.")

    resolved = resolve_project_path((root or Path.cwd()).resolve(), path)
    if resolved is None:  # pragma: no cover - path is never None here
        raise ValueError("must be a path value.")
    return resolved


def to_raw_path(value: object, *, name: str = "value") -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    raise ValueError(f"{name} must be a non-blank path.")


def to_url_path(value: object, *, name: str = "value") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string.")
    return f"/{value.strip('/')}"


__all__ = ("to_bool", "to_path", "to_raw_path", "to_url_path")
