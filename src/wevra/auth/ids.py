from __future__ import annotations

from uuid import UUID


def parse_uuid(value: str | UUID) -> UUID | None:
    if isinstance(value, UUID):
        return value

    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
