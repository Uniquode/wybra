from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from wybra.forms.fields import Form, FormError, UnknownFieldPolicy


class ModelFormError(FormError):
    """Base for model-backed form errors."""


class ModelFormDeclarationError(ModelFormError):
    """Raised when a model form declaration is invalid."""


class ModelBindingError(ModelFormError):
    """Raised when a model binding cannot read or write a record."""


class Binding:
    """Base class for model form field bindings."""

    writable = True

    def for_field(self, field_name: str) -> Binding:
        return self

    def read(self, instance: object) -> object:
        raise NotImplementedError

    def write(self, instance: object, value: object) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Attr(Binding):
    name: str

    def read(self, instance: object) -> object:
        try:
            return getattr(instance, self.name)
        except AttributeError as exc:
            raise ModelBindingError(
                f"Model instance has no attribute for binding: {self.name}."
            ) from exc

    def write(self, instance: object, value: object) -> None:
        if not hasattr(instance, self.name):
            raise ModelBindingError(
                f"Model instance has no attribute for binding: {self.name}."
            )
        setattr(instance, self.name, value)


@dataclass(frozen=True, slots=True)
class JsonPath(Binding):
    attribute: str
    keys: tuple[str, ...]

    def __init__(self, attribute: str, *keys: str) -> None:
        if not keys:
            raise ModelFormDeclarationError("JsonPath requires at least one key.")
        object.__setattr__(self, "attribute", attribute)
        object.__setattr__(self, "keys", keys)

    def read(self, instance: object) -> object:
        value = Attr(self.attribute).read(instance)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ModelBindingError(
                f"Model attribute is not a mapping for JsonPath: {self.attribute}."
            )
        return self._read_path(cast(Mapping[str, object], value))

    def write(self, instance: object, value: object) -> None:
        if not hasattr(instance, self.attribute):
            raise ModelBindingError(
                f"Model instance has no attribute for binding: {self.attribute}."
            )
        current = getattr(instance, self.attribute)
        if current is None:
            current = {}
        if not isinstance(current, Mapping):
            raise ModelBindingError(
                f"Model attribute is not a mapping for JsonPath: {self.attribute}."
            )
        setattr(instance, self.attribute, self._updated_mapping(current, value))

    def _read_path(self, value: Mapping[str, object]) -> object:
        current: object = value
        for key in self.keys:
            if not isinstance(current, Mapping):
                raise ModelBindingError(
                    f"Model JsonPath segment is not a mapping: {key}."
                )
            current_mapping = cast(Mapping[str, object], current)
            if key not in current_mapping:
                return None
            current = current_mapping[key]
        return current

    def _updated_mapping(
        self,
        source: Mapping[object, object],
        value: object,
    ) -> dict[object, object]:
        updated: dict[object, object] = dict(source)
        cursor = updated
        for key in self.keys[:-1]:
            nested = cursor.get(key)
            if nested is None:
                nested = {}
            if not isinstance(nested, Mapping):
                raise ModelBindingError(
                    f"Model JsonPath segment is not a mapping: {key}."
                )
            copied = dict(nested)
            cursor[key] = copied
            cursor = copied
        cursor[self.keys[-1]] = value
        return updated


@dataclass(frozen=True, slots=True)
class ReadOnly(Binding):
    binding: Binding | None = None
    writable = False

    def for_field(self, field_name: str) -> ReadOnly:
        if self.binding is None:
            return ReadOnly(Attr(field_name))
        return self

    def read(self, instance: object) -> object:
        if self.binding is None:
            raise ModelFormDeclarationError("ReadOnly binding is not attached.")
        return self.binding.read(instance)

    def write(self, instance: object, value: object) -> None:
        return None


class ModelForm(Form):
    def __init__(
        self,
        *,
        instance: object | None = None,
        defaults: Mapping[str, object] | None = None,
        values: Mapping[str, object] | None = None,
        options: Mapping[str, Mapping[str, str]] | None = None,
        unknown_fields: UnknownFieldPolicy = "ignore",
    ) -> None:
        self.instance = instance
        self.model = self._declared_model()
        self.bindings = self._declared_bindings()
        bound_defaults = self._bound_defaults(defaults or {}, instance)
        super().__init__(
            defaults=bound_defaults,
            values=values,
            options=options,
            unknown_fields=unknown_fields,
        )

    def apply(self, instance: object | None = None) -> object:
        target = instance if instance is not None else self.instance
        if target is None:
            raise ModelBindingError("ModelForm.apply() requires an instance.")
        if not self.is_valid():
            return target

        for name, result in self.result.fields.items():
            form_field = self.fields[name]
            binding = self.bindings[name]
            if form_field.disabled or not binding.writable or not result.is_valid:
                continue
            binding.write(target, result.value)
        return target

    def _bound_defaults(
        self,
        defaults: Mapping[str, object],
        instance: object | None,
    ) -> dict[str, object]:
        bound_defaults = dict(defaults)
        if instance is not None:
            bound_defaults.update(
                {
                    name: binding.read(instance)
                    for name, binding in self.bindings.items()
                }
            )
        return bound_defaults

    @classmethod
    def _declared_model(cls) -> object:
        model = getattr(getattr(cls, "Meta", None), "model", None)
        if model is None:
            raise ModelFormDeclarationError(
                "ModelForm declarations require Meta.model."
            )
        return model

    @classmethod
    def _declared_bindings(cls) -> dict[str, Binding]:
        fields = cls.declared_fields()
        raw_bindings = getattr(getattr(cls, "Meta", None), "bindings", {}) or {}
        if not isinstance(raw_bindings, Mapping):
            raise ModelFormDeclarationError("Meta.bindings must be a mapping.")

        unknown = set(raw_bindings) - set(fields)
        if unknown:
            raise ModelFormDeclarationError(
                "Unknown binding field(s): " + ", ".join(sorted(unknown))
            )

        return {
            name: cls._binding_for(name, raw_bindings.get(name, Attr(name)))
            for name in fields
        }

    @staticmethod
    def _binding_for(field_name: str, binding: object) -> Binding:
        if isinstance(binding, str):
            return Attr(binding)
        if isinstance(binding, Binding):
            return binding.for_field(field_name)
        raise ModelFormDeclarationError(
            f"Unsupported binding declaration for field: {field_name}."
        )


__all__ = (
    "Attr",
    "Binding",
    "JsonPath",
    "ModelBindingError",
    "ModelForm",
    "ModelFormDeclarationError",
    "ModelFormError",
    "ReadOnly",
)
