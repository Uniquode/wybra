from __future__ import annotations

from time import time


def current_timestamp() -> float:
    """Return the current UTC instant as Unix timestamp seconds."""
    return time()


__all__ = ("current_timestamp",)
