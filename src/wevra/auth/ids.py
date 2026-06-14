from __future__ import annotations

from uuid import UUID


def parse_uuid(value: str | UUID) -> UUID | None:
    if isinstance(value, UUID):
        return value

    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def log_safe_uuid(value: str | UUID) -> str:
    parsed = parse_uuid(value)
    if parsed is None:
        return "<invalid-uuid>"

    return str(parsed)


def log_safe_line(value: object) -> str:
    return str(value).replace("\r", "").replace("\n", "")
