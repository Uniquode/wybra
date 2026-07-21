"""Session event contracts and secret-safe lifecycle descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    COMPLETED,
    EVT_SESSION,
    SESSION,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionLifecycleEvent(Event):
    """A session lifecycle outcome without identifiers or payloads."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    backend: str
    outcome: str
    error_type: str | None = None


def session_event(
    call: BoundArguments, observation: EventOutcome | None
) -> Event | None:
    """Build a session lifecycle event from opaque operation metadata."""

    if observation is None:
        return None
    context = call.arguments["self"]
    operation = call.arguments["operation"]
    outcome = call.arguments["outcome"]
    error = call.arguments.get("error")
    if not isinstance(operation, str) or not isinstance(outcome, str):
        raise TypeError("Session events require string operation and outcome values.")
    storage = getattr(context, "storage", None)
    return SessionLifecycleEvent(
        topic=EVT_SESSION(SESSION, COMPLETED),
        operation=operation,
        backend=type(storage).__name__,
        outcome=outcome,
        error_type=type(error).__name__ if isinstance(error, Exception) else None,
    )


__all__ = ("session_event",)
