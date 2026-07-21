"""Template event contracts and safe descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    COMPLETED,
    EVT_TEMPLATE,
    TEMPLATE_RENDER,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateRenderCompletedEvent(Event):
    """An observation of one asynchronous template-rendering outcome."""

    kind: ClassVar[EventSegment] = COMPLETED
    template_name: str
    duration_seconds: float
    error_type: str | None = None


def template_event(
    call: BoundArguments,
    outcome: EventOutcome | None,
) -> Event | None:
    """Build a terminal template event without inspecting its context values."""

    if outcome is None:
        return None
    template_name = call.arguments["template_name"]
    if not isinstance(template_name, str):
        raise TypeError("Template events require a template name.")
    return TemplateRenderCompletedEvent(
        topic=EVT_TEMPLATE(TEMPLATE_RENDER, COMPLETED),
        template_name=template_name,
        duration_seconds=outcome.duration_seconds,
        error_type=outcome.error_type,
    )


__all__ = ("template_event",)
