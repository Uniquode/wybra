"""View event contracts and safe descriptors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import BoundArguments
from typing import Any, ClassVar, cast

from fastapi import Request
from fastapi.responses import Response

from wybra.errors.diagnostics import type_name
from wybra.events._core import (
    BULK,
    COLLECTION,
    COMPLETED,
    DISPATCH,
    EVT_ROUTE,
    EVT_VIEW,
    GENERIC,
    ITEM,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteDispatchedEvent(Event):
    """An observation of a resolved route entering a class-based view."""

    kind: ClassVar[EventSegment] = DISPATCH
    method: str
    route_name: str | None
    view_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewCompletedEvent(Event):
    """An observation of a class-based view dispatch outcome."""

    kind: ClassVar[EventSegment] = COMPLETED
    method: str
    route_name: str | None
    view_type: str
    duration_seconds: float
    status_code: int | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GenericViewCompletedEvent(Event):
    """An observation of a model-driven collection, item, or bulk operation."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    model_type: str | None
    content_type: str | None
    duration_seconds: float
    status_code: int | None
    affected_count: int | None = None
    skipped_count: int | None = None
    failed_count: int | None = None
    error_type: str | None = None


def view_event(call: BoundArguments, outcome: EventOutcome | None) -> Event:
    """Build route-entry and terminal view events from an ordinary dispatch."""

    request = _request(call)
    view = call.arguments["self"]
    route_name = _route_name(request)
    view_type = type_name(type(view))
    if outcome is None:
        return RouteDispatchedEvent(
            topic=EVT_ROUTE(DISPATCH),
            method=request.method,
            route_name=route_name,
            view_type=view_type,
        )
    response = outcome.result
    return ViewCompletedEvent(
        topic=EVT_VIEW(COMPLETED),
        method=request.method,
        route_name=route_name,
        view_type=view_type,
        duration_seconds=outcome.duration_seconds,
        status_code=response.status_code if isinstance(response, Response) else None,
        error_type=outcome.error_type,
    )


def generic_view_event(
    call: BoundArguments, outcome: EventOutcome | None
) -> Event | None:
    """Build a terminal generic-view event from private view state only."""

    if outcome is None:
        return None
    request = _request(call)
    view = call.arguments["self"]
    kwargs = call.arguments["kwargs"]
    if not isinstance(kwargs, Mapping):
        raise TypeError("Generic view events require keyword dispatch arguments.")
    operation = _generic_operation(kwargs)
    affected_count, skipped_count, failed_count = _event_counts(
        getattr(view, "_event_bulk_counts", None)
    )
    response = outcome.result
    model_type = _safe_call(view, "_model_type_identity")
    content_type = _safe_call(view, "_content_type_identifier", request)
    return GenericViewCompletedEvent(
        topic=EVT_VIEW(GENERIC, _operation_segment(operation), COMPLETED),
        operation=operation,
        model_type=model_type if isinstance(model_type, str) else None,
        content_type=content_type if isinstance(content_type, str) else None,
        duration_seconds=outcome.duration_seconds,
        status_code=response.status_code if isinstance(response, Response) else None,
        affected_count=affected_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        error_type=outcome.error_type,
    )


def _request(call: BoundArguments) -> Request:
    request = call.arguments["request"]
    if not isinstance(request, Request):
        raise TypeError("View events require a FastAPI request.")
    return request


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    value = getattr(route, "name", None)
    return value if isinstance(value, str) and value else None


def _generic_operation(kwargs: Mapping[str, Any]) -> str:
    if kwargs.get("bulk"):
        return "bulk"
    if kwargs.get("id") is not None:
        return "item"
    return "collection"


def _operation_segment(operation: str) -> EventSegment:
    return {"bulk": BULK, "collection": COLLECTION, "item": ITEM}[operation]


def _event_counts(
    counts: object,
) -> tuple[int | None, int | None, int | None]:
    if not isinstance(counts, tuple) or len(counts) != 3:
        return (None, None, None)
    if not all(isinstance(count, int) for count in counts):
        return (None, None, None)
    return cast(tuple[int, int, int], counts)


def _safe_call(value: object, name: str, *args: object) -> object | None:
    method = getattr(value, name, None)
    if not callable(method):
        return None
    try:
        return method(*args)
    except Exception:
        return None


__all__ = ("generic_view_event", "view_event")
