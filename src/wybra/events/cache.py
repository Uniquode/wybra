"""Private cache event contracts and safe payload conversion."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    COMPLETED,
    EVT_CACHE,
    FAILED,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheOperationCompletedEvent(Event):
    """A completed cache operation without its raw key or value."""

    kind: ClassVar[EventSegment] = COMPLETED
    owner: str
    key_fingerprint: str
    outcome: str
    duration_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheOperationFailedEvent(Event):
    """A failed cache operation without its raw key or value."""

    kind: ClassVar[EventSegment] = FAILED
    owner: str
    key_fingerprint: str
    operation: str
    duration_seconds: float
    error_type: str


def cache_event(
    call: BoundArguments,
    _outcome: EventOutcome | None,
) -> Event | None:
    """Build a safe cache event from one internal record-operation call."""

    if _outcome is None:
        return None

    arguments = call.arguments
    operation = arguments["operation"]
    owner = arguments["owner"]
    key = arguments["key"]
    started = arguments["started"]
    if not isinstance(operation, str):
        raise TypeError("Cache event operations must use string values.")
    if not isinstance(owner, str) or not isinstance(key, str):
        raise TypeError("Cache event owner and key must be strings.")
    if not isinstance(started, float):
        raise TypeError("Cache event start times must be floats.")
    duration_seconds = time.perf_counter() - started
    error = arguments.get("error")
    if isinstance(error, BaseException):
        return CacheOperationFailedEvent(
            topic=EVT_CACHE(operation, FAILED),
            owner=owner,
            key_fingerprint=key_fingerprint(owner, key),
            operation=operation,
            duration_seconds=duration_seconds,
            error_type=type(error).__name__,
        )
    outcome = arguments.get("outcome")
    if not isinstance(outcome, str):
        raise TypeError("Completed cache events require a string outcome.")
    return CacheOperationCompletedEvent(
        topic=EVT_CACHE(operation, COMPLETED),
        owner=owner,
        key_fingerprint=key_fingerprint(owner, key),
        outcome=outcome,
        duration_seconds=duration_seconds,
    )


def key_fingerprint(owner: str, key: str) -> str:
    """Return a stable cache-key fingerprint without retaining its contents."""

    return hashlib.sha256(f"{owner}:{key}".encode()).hexdigest()[:16]


__all__ = ("cache_event",)
