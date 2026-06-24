from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, time
from types import MappingProxyType
from typing import Any, Literal, Protocol, Self, runtime_checkable

type UnknownFieldPolicy = Literal["ignore", "error"]
type FormErrorKey = str | None

MARKUP_ERROR = "Enter plain text without HTML or markup."
UNSAFE_CONTROL_CHARACTER_ERROR = "Enter text without unsafe control characters."
_MARKUP_PATTERN = re.compile(
    r"<!--|--!?>|<![A-Za-z]|<\?|\?>|</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?>"
)
_ALLOWED_CONTROL_CHARACTERS = {"\t", "\n", "\r"}


@runtime_checkable
class HasGetList(Protocol):
    def getlist(self, key: str | None = None) -> list[object]: ...


class FormError(ValueError):
    """Base for declarative form errors."""


class UnknownFormFieldError(FormError):
    """Base for errors that reference a field the form does not declare."""


class UnknownInitialFieldError(UnknownFormFieldError):
    """Raised when initial form mappings reference undeclared fields."""


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str
    selected: bool = False


@dataclass(frozen=True, slots=True)
class FieldResult:
    name: str
    raw_value: object = None
    value: object = None
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class FormResult:
    fields: Mapping[str, FieldResult]
    unknown_fields: tuple[str, ...] = ()
    form_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def values(self) -> dict[str, object]:
        return {
            name: result.value
            for name, result in self.fields.items()
            if result.is_valid and result.value is not None
        }

    @property
    def errors(self) -> dict[FormErrorKey, tuple[str, ...]]:
        errors: dict[FormErrorKey, tuple[str, ...]] = {
            name: result.errors for name, result in self.fields.items() if result.errors
        }
        if self.form_errors:
            errors[None] = self.form_errors
        return errors

    @property
    def is_valid(self) -> bool:
        return not self.form_errors and all(
            result.is_valid for result in self.fields.values()
        )


@dataclass(slots=True)
class Field:
    label: str | None = None
    required: bool = True
    disabled: bool = False
    help_text: str | None = None
    widget: str | None = None
    name: str = dataclass_field(default="", init=False)
    value: object = dataclass_field(default=None, init=False)
    raw_value: object = dataclass_field(default=None, init=False)
    errors: tuple[str, ...] = dataclass_field(default=(), init=False)

    @property
    def widget_name(self) -> str:
        return self.widget or self.default_widget

    @property
    def default_widget(self) -> str:
        return "text"

    def bind(self, name: str, value: object = None) -> Self:
        bound = deepcopy(self)
        bound.name = name
        bound.label = self.label or label_from_name(name)
        bound.value = value
        return bound

    def parse(self, raw_value: object) -> FieldResult:
        if self.disabled:
            return self._accepted(None, raw_value=None)
        if self._is_empty(raw_value):
            if self.required:
                return self._rejected(raw_value, "This field is required.")
            return self._accepted(None, raw_value=raw_value)
        try:
            return self._accepted(self.to_python(raw_value), raw_value=raw_value)
        except ValueError as exc:
            return self._rejected(raw_value, str(exc))

    def to_python(self, raw_value: object) -> object:
        return text_value(raw_value)

    def options(self) -> tuple[Option, ...]:
        return ()

    def with_result(self, result: FieldResult) -> None:
        self.raw_value = result.raw_value
        if self.disabled:
            self.errors = result.errors
            return
        if result.value is not None:
            self.value = result.value
        elif result.errors and isinstance(result.raw_value, str):
            self.value = result.raw_value
        else:
            self.value = None
        self.errors = result.errors

    def _accepted(self, value: object, *, raw_value: object) -> FieldResult:
        return FieldResult(name=self.name, raw_value=raw_value, value=value)

    def _rejected(self, raw_value: object, message: str) -> FieldResult:
        return FieldResult(name=self.name, raw_value=raw_value, errors=(message,))

    @staticmethod
    def _is_empty(raw_value: object) -> bool:
        return raw_value is None or raw_value == "" or raw_value == ()


class TextField(Field):
    @property
    def default_widget(self) -> str:
        return "text"

    def to_python(self, raw_value: object) -> str:
        value = text_value(raw_value)
        if has_unsafe_control_character(value):
            raise ValueError(UNSAFE_CONTROL_CHARACTER_ERROR)
        if not self.allow_html and has_markup(value):
            raise ValueError(MARKUP_ERROR)
        max_length = getattr(self, "max_length", None)
        if isinstance(max_length, int) and len(value) > max_length:
            raise ValueError(f"Must be {max_length} characters or fewer.")
        return value

    def __init__(
        self,
        *,
        label: str | None = None,
        required: bool = True,
        disabled: bool = False,
        help_text: str | None = None,
        widget: str | None = None,
        max_length: int | None = None,
        allow_html: bool = False,
    ) -> None:
        super().__init__(
            label=label,
            required=required,
            disabled=disabled,
            help_text=help_text,
            widget=widget,
        )
        self.max_length = max_length
        self.allow_html = allow_html


class TextAreaField(TextField):
    @property
    def default_widget(self) -> str:
        return "textarea"


class HiddenField(TextField):
    @property
    def default_widget(self) -> str:
        return "hidden"


class PositiveIntegerField(Field):
    @property
    def default_widget(self) -> str:
        return "number"

    def to_python(self, raw_value: object) -> int:
        try:
            value = int(text_value(raw_value))
        except ValueError as exc:
            raise ValueError("Enter a positive integer.") from exc
        if value <= 0:
            raise ValueError("Enter a positive integer.")
        return value


class DateField(Field):
    @property
    def default_widget(self) -> str:
        return "date"

    def to_python(self, raw_value: object) -> date:
        try:
            return date.fromisoformat(text_value(raw_value))
        except ValueError as exc:
            raise ValueError("Enter a valid date.") from exc


class TimeField(Field):
    @property
    def default_widget(self) -> str:
        return "time"

    def to_python(self, raw_value: object) -> time:
        try:
            return time.fromisoformat(text_value(raw_value))
        except ValueError as exc:
            raise ValueError("Enter a valid time.") from exc


class DateTimeField(Field):
    @property
    def default_widget(self) -> str:
        return "datetime"

    def to_python(self, raw_value: object) -> datetime:
        try:
            return datetime.fromisoformat(text_value(raw_value))
        except ValueError as exc:
            raise ValueError("Enter a valid date and time.") from exc


class ChoiceField(Field):
    choices: Mapping[str, str]

    def __init__(
        self,
        *,
        choices: Mapping[str, str],
        label: str | None = None,
        required: bool = True,
        disabled: bool = False,
        help_text: str | None = None,
        widget: str | None = None,
    ) -> None:
        super().__init__(
            label=label,
            required=required,
            disabled=disabled,
            help_text=help_text,
            widget=widget,
        )
        self.choices = dict(choices)

    @property
    def default_widget(self) -> str:
        return "select"

    def to_python(self, raw_value: object) -> str:
        value = text_value(raw_value)
        if value not in self.choices:
            raise ValueError("Select a valid option.")
        return value

    def options(self) -> tuple[Option, ...]:
        selected = str(self.value) if self.value is not None else ""
        return tuple(
            Option(value=value, label=label, selected=value == selected)
            for value, label in self.choices.items()
        )


class SelectField(ChoiceField):
    def __init__(self, *, choices: Mapping[str, str] | None = None, **kwargs: Any):
        super().__init__(choices=choices or {}, **kwargs)


class RadioField(SelectField):
    @property
    def default_widget(self) -> str:
        return "radio"


class MultiSelectField(SelectField):
    @property
    def default_widget(self) -> str:
        return "multiselect"

    def parse(self, raw_value: object) -> FieldResult:
        values = tuple(text_value(value) for value in list_values(raw_value))
        if not values and self.required:
            return self._rejected(raw_value, "This field is required.")
        invalid = tuple(value for value in values if value not in self.choices)
        if invalid:
            return self._rejected(raw_value, "Select a valid option.")
        return self._accepted(values, raw_value=raw_value)

    def options(self) -> tuple[Option, ...]:
        selected = set(self.value) if isinstance(self.value, tuple) else set()
        return tuple(
            Option(value=value, label=label, selected=value in selected)
            for value, label in self.choices.items()
        )


class CheckboxField(Field):
    @property
    def default_widget(self) -> str:
        return "checkbox"

    def to_python(self, raw_value: object) -> bool:
        return bool_value(raw_value)


class SwitchField(CheckboxField):
    @property
    def default_widget(self) -> str:
        return "switch"


class FileUploadField(Field):
    @property
    def default_widget(self) -> str:
        return "file"

    def parse(self, raw_value: object) -> FieldResult:
        if self.disabled:
            return self._accepted(None, raw_value=None)
        if self._is_empty_file(raw_value):
            if self.required:
                return self._rejected(raw_value, "This field is required.")
            return self._accepted(None, raw_value=raw_value)
        return self._accepted(raw_value, raw_value=raw_value)

    @staticmethod
    def _is_empty_file(raw_value: object) -> bool:
        if Field._is_empty(raw_value):
            return True
        filename = getattr(raw_value, "filename", None)
        return filename == ""


class SliderField(PositiveIntegerField):
    def __init__(
        self,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        label: str | None = None,
        required: bool = True,
        disabled: bool = False,
        help_text: str | None = None,
        widget: str | None = None,
    ) -> None:
        super().__init__(
            label=label,
            required=required,
            disabled=disabled,
            help_text=help_text,
            widget=widget,
        )
        self.min_value = min_value
        self.max_value = max_value

    @property
    def default_widget(self) -> str:
        return "slider"

    def to_python(self, raw_value: object) -> int:
        try:
            value = int(text_value(raw_value))
        except ValueError as exc:
            raise ValueError("Enter an integer.") from exc
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"Must be at least {self.min_value}.")
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"Must be at most {self.max_value}.")
        return value


class Form:
    def __init__(
        self,
        *,
        defaults: Mapping[str, object] | None = None,
        values: Mapping[str, object] | None = None,
        options: Mapping[str, Mapping[str, str]] | None = None,
        unknown_fields: UnknownFieldPolicy = "ignore",
    ) -> None:
        self.unknown_fields = unknown_fields
        self.fields = self._bind_fields(defaults or {}, values or {}, options or {})
        self.errors: dict[FormErrorKey, list[str]] = {}
        self.values: dict[str, object] = {
            name: form_field.value
            for name, form_field in self.fields.items()
            if form_field.value is not None
        }
        self.raw_values: dict[str, object] = {}
        self.field_results: dict[str, FieldResult] = {
            name: FieldResult(name=name, value=form_field.value)
            for name, form_field in self.fields.items()
        }
        self._defer_result_sync = False
        self._result = FormResult(
            fields={
                name: FieldResult(name=name, value=form_field.value)
                for name, form_field in self.fields.items()
            }
        )

    @classmethod
    def declared_fields(cls) -> dict[str, Field]:
        fields: dict[str, Field] = {}
        for form_class in reversed(cls.mro()):
            for name, value in vars(form_class).items():
                if isinstance(value, Field):
                    fields[name] = value
        return fields

    def parse(self, data: Mapping[str, object]) -> FormResult:
        results: dict[str, FieldResult] = {}
        self.errors = {}
        self.raw_values = {}
        self.values = {}
        unknown = tuple(name for name in data if name not in self.fields)
        self._pending_form_errors = (
            ("Unknown submitted field(s): " + ", ".join(sorted(unknown)),)
            if unknown and self.unknown_fields == "error"
            else ()
        )
        for name, form_field in self.fields.items():
            raw_value = form_raw_value(data, name)
            result = form_field.parse(raw_value)
            form_field.with_result(result)
            results[name] = result
            self.raw_values[name] = raw_value
            if result.is_valid and result.value is not None:
                self.values[name] = result.value
        self.field_results = results
        self._defer_result_sync = True
        try:
            for name in self.fields:
                self.validate(name)
            self.validate(None)
        finally:
            self._defer_result_sync = False
        results = self._results_with_errors(results)
        self._result = FormResult(
            fields=results,
            unknown_fields=unknown,
            form_errors=tuple(self.errors.get(None, ())),
        )
        del self._pending_form_errors
        return self.result

    def validate(self, field_name: str | None = None) -> bool:
        if field_name is None:
            for message in getattr(self, "_pending_form_errors", ()):
                self.add_error(None, message)
            return not self.errors

        result = self.field_results.get(field_name)
        if result is not None:
            for message in result.errors:
                self.add_error(field_name, message)
        return field_name not in self.errors

    def add_error(self, field_name: str | None, message: str) -> None:
        messages = self.errors.setdefault(field_name, [])
        if message not in messages:
            messages.append(message)
            if not self._defer_result_sync:
                self._sync_result_errors()

    def is_valid(self) -> bool:
        return not self.errors

    @property
    def result(self) -> FormResult:
        return self._result

    def _bind_fields(
        self,
        defaults: Mapping[str, object],
        values: Mapping[str, object],
        options: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Field]:
        fields: dict[str, Field] = {}
        declared = self.declared_fields()
        unknown_values = (set(defaults) | set(values) | set(options)) - set(declared)
        if unknown_values and self.unknown_fields == "error":
            unknown = ", ".join(sorted(unknown_values))
            raise UnknownInitialFieldError(f"Unknown initial field value(s): {unknown}")
        for name, form_field in declared.items():
            value = values.get(name, defaults.get(name))
            bound = form_field.bind(name, value)
            if isinstance(bound, SelectField) and name in options:
                bound.choices = dict(options[name])
            fields[name] = bound
        return fields

    def _results_with_errors(
        self,
        results: Mapping[str, FieldResult],
    ) -> dict[str, FieldResult]:
        updated_results: dict[str, FieldResult] = {}
        for name, result in results.items():
            field_errors = tuple(self.errors.get(name, ()))
            updated = FieldResult(
                name=result.name,
                raw_value=result.raw_value,
                value=result.value,
                errors=field_errors,
            )
            self.fields[name].with_result(updated)
            updated_results[name] = updated
        self.field_results = updated_results
        return updated_results

    def _sync_result_errors(self) -> None:
        results = self._results_with_errors(self.field_results)
        self._result = FormResult(
            fields=results,
            unknown_fields=self.result.unknown_fields,
            form_errors=tuple(self.errors.get(None, ())),
        )


def label_from_name(name: str) -> str:
    return name.replace("_", " ").capitalize()


def text_value(raw_value: object) -> str:
    if isinstance(raw_value, str):
        return raw_value
    if isinstance(raw_value, bytes | bytearray):
        try:
            return bytes(raw_value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Enter a valid text value.") from exc
    if raw_value is None:
        return ""
    return str(raw_value)


def has_markup(value: str) -> bool:
    return _MARKUP_PATTERN.search(value) is not None


def has_unsafe_control_character(value: str) -> bool:
    return any(
        character not in _ALLOWED_CONTROL_CHARACTERS
        and (ord(character) < 32 or 127 <= ord(character) <= 159)
        for character in value
    )


def bool_value(raw_value: object) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.lower() in {"1", "true", "yes", "on"}
    return bool(raw_value)


def list_values(raw_value: object) -> tuple[object, ...]:
    if isinstance(raw_value, HasGetList):
        return tuple(raw_value.getlist())
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, str):
        return tuple(raw_value)
    if raw_value is None or raw_value == "":
        return ()
    return (raw_value,)


def form_raw_value(data: Mapping[str, object], name: str) -> object:
    if isinstance(data, HasGetList):
        values = data.getlist(name)
        if len(values) > 1:
            return tuple(values)
    return data.get(name)


__all__ = (
    "CheckboxField",
    "ChoiceField",
    "DateField",
    "DateTimeField",
    "Field",
    "FieldResult",
    "FileUploadField",
    "Form",
    "FormError",
    "FormResult",
    "HiddenField",
    "MultiSelectField",
    "Option",
    "PositiveIntegerField",
    "RadioField",
    "SelectField",
    "SliderField",
    "SwitchField",
    "TextAreaField",
    "TextField",
    "TimeField",
    "UnknownFormFieldError",
    "UnknownInitialFieldError",
)
