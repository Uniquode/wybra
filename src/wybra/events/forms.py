"""Form event contracts and secret-safe descriptors."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    COMPLETED,
    EVT_FORM,
    FAILED,
    PERSISTENCE,
    VALIDATION,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FormValidationCompletedEvent(Event):
    """An observation of a form validation attempt without submitted values."""

    kind: ClassVar[EventSegment] = COMPLETED
    form_type: str
    field_count: int
    invalid_field_count: int
    valid: bool
    duration_seconds: float
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FormPersistenceCompletedEvent(Event):
    """An observation of a form persistence outcome without model values."""

    kind: ClassVar[EventSegment] = COMPLETED
    form_type: str
    model_types: tuple[str, ...]
    operation: str
    changed_fields: tuple[str, ...]
    affected_count: int
    created: bool
    updated: bool
    deleted: bool
    stale_conflict: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class FormPersistenceFailedEvent(Event):
    """An observation of a failed form persistence operation."""

    kind: ClassVar[EventSegment] = FAILED
    form_type: str
    model_types: tuple[str, ...]
    operation: str
    duration_seconds: float
    error_type: str


def validation_event(
    call: BoundArguments,
    outcome: EventOutcome | None,
) -> Event | None:
    """Build a terminal validation event without retaining submitted values."""

    if outcome is None:
        return None
    form = call.arguments["self"]
    fields = getattr(form, "fields", {})
    field_results = getattr(form, "field_results", {})
    errors = getattr(form, "errors", {})
    if not isinstance(fields, dict) or not isinstance(field_results, dict):
        raise TypeError("Form validation events require a bound form.")
    invalid_fields = sum(
        not bool(getattr(result, "is_valid", False))
        for result in field_results.values()
    )
    is_valid = getattr(form, "is_valid", None)
    return FormValidationCompletedEvent(
        topic=EVT_FORM(VALIDATION, COMPLETED),
        form_type=_form_type_identifier(form),
        field_count=len(fields),
        invalid_field_count=invalid_fields + int(bool(errors.get(None))),
        valid=bool(is_valid()) if callable(is_valid) else False,
        duration_seconds=outcome.duration_seconds,
        error_type=outcome.error_type,
    )


def persistence_event(
    call: BoundArguments,
    outcome: EventOutcome | None,
    operation: str = "save",
) -> Event | None:
    """Build a terminal persistence event from opaque form and result metadata."""

    if outcome is None:
        return None
    form = call.arguments["self"]
    models = _model_types(form)
    if not outcome.succeeded:
        return FormPersistenceFailedEvent(
            topic=EVT_FORM(PERSISTENCE, FAILED),
            form_type=_form_type_identifier(form),
            model_types=models,
            operation=operation,
            duration_seconds=outcome.duration_seconds,
            error_type=outcome.error_type or "Exception",
        )
    result = outcome.result
    return FormPersistenceCompletedEvent(
        topic=EVT_FORM(PERSISTENCE, COMPLETED),
        form_type=_form_type_identifier(form),
        model_types=models,
        operation=operation,
        changed_fields=_tuple_attribute(result, "changed_fields"),
        affected_count=_int_attribute(result, "affected_count"),
        created=_bool_attribute(result, "created"),
        updated=_bool_attribute(result, "updated"),
        deleted=_bool_attribute(result, "deleted"),
        stale_conflict=bool(getattr(form, "_stale_conflict", False)),
        duration_seconds=outcome.duration_seconds,
    )


def _form_type_identifier(form: object) -> str:
    form_type = type(form)
    return f"{form_type.__module__}.{form_type.__qualname__}"


def _model_types(form: object) -> tuple[str, ...]:
    members = getattr(form, "members", None)
    if isinstance(members, tuple):
        models = tuple(getattr(member, "model", None) for member in members)
        return _model_type_identifiers(
            model for model in models if isinstance(model, type)
        )
    declared_model = getattr(form, "_declared_model", None)
    if callable(declared_model):
        model = declared_model()
        if isinstance(model, type):
            return _model_type_identifiers((model,))
    return ()


def _model_type_identifiers(models: Iterable[type[object]]) -> tuple[str, ...]:
    return tuple(f"{model.__module__}.{model.__qualname__}" for model in models)


def _tuple_attribute(value: object | None, name: str) -> tuple[str, ...]:
    attribute = getattr(value, name, ())
    if not isinstance(attribute, tuple) or not all(
        isinstance(item, str) for item in attribute
    ):
        return ()
    return attribute


def _int_attribute(value: object | None, name: str) -> int:
    attribute = getattr(value, name, 0)
    return attribute if isinstance(attribute, int) else 0


def _bool_attribute(value: object | None, name: str) -> bool:
    return bool(getattr(value, name, False))


__all__ = ("persistence_event", "validation_event")
