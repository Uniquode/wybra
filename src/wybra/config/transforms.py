from __future__ import annotations

import math
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


def to_non_blank_string(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("must be a non-blank string.")


def to_optional_non_blank_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("must be a non-blank string when configured.")


def to_positive_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("must be a positive number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a positive number.") from exc
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError("must be a positive number.")
    return parsed


def to_optional_positive_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return to_positive_float(value)
    except ValueError as exc:
        raise ValueError("must be a positive number when configured.") from exc


def to_positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("must be a positive integer.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError("must be a positive integer.") from exc
    else:
        raise ValueError("must be a positive integer.")
    if parsed <= 0:
        raise ValueError("must be a positive integer.")
    return parsed


__all__ = (
    "to_bool",
    "to_non_blank_string",
    "to_optional_non_blank_string",
    "to_optional_positive_float",
    "to_path",
    "to_positive_float",
    "to_positive_int",
    "to_raw_path",
    "to_url_path",
)
