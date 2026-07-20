"""Secret-safe event helpers shared by form persistence implementations."""

from __future__ import annotations

from collections.abc import Iterable

from wybra.events import (
    EVT_FORM,
    PERSISTENCE,
    EventsCapability,
    FormPersistenceCompletedEvent,
    FormPersistenceFailedEvent,
    publish_observation,
    scoped,
)


def form_type_identifier(form: object) -> str:
    """Return a stable type identity without inspecting a form's values."""

    form_type = type(form)
    return f"{form_type.__module__}.{form_type.__qualname__}"


def model_type_identifiers(models: Iterable[type[object]]) -> tuple[str, ...]:
    """Return stable model type identities without exposing instances."""

    return tuple(f"{model.__module__}.{model.__qualname__}" for model in models)


async def publish_persistence_completed(
    events: EventsCapability | None,
    *,
    form: object,
    models: Iterable[type[object]],
    operation: str,
    changed_fields: tuple[str, ...],
    affected_count: int,
    created: bool,
    updated: bool,
    deleted: bool,
    stale_conflict: bool,
    duration_seconds: float,
) -> None:
    """Publish a non-controlling completed persistence observation."""

    if events is None:
        return
    with scoped(EVT_FORM(PERSISTENCE)):
        await publish_observation(
            events,
            FormPersistenceCompletedEvent(
                form_type=form_type_identifier(form),
                model_types=model_type_identifiers(models),
                operation=operation,
                changed_fields=changed_fields,
                affected_count=affected_count,
                created=created,
                updated=updated,
                deleted=deleted,
                stale_conflict=stale_conflict,
                duration_seconds=duration_seconds,
            ),
            message="form persistence event",
        )


async def publish_persistence_failed(
    events: EventsCapability | None,
    *,
    form: object,
    models: Iterable[type[object]],
    operation: str,
    duration_seconds: float,
    error: Exception,
) -> None:
    """Publish a non-controlling failed persistence observation."""

    if events is None:
        return
    with scoped(EVT_FORM(PERSISTENCE)):
        await publish_observation(
            events,
            FormPersistenceFailedEvent(
                form_type=form_type_identifier(form),
                model_types=model_type_identifiers(models),
                operation=operation,
                duration_seconds=duration_seconds,
                error_type=type(error).__name__,
            ),
            message="form persistence failure event",
        )


__all__ = (
    "form_type_identifier",
    "model_type_identifiers",
    "publish_persistence_completed",
    "publish_persistence_failed",
)
