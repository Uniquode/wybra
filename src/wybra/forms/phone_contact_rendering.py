from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup

from wybra.forms.fields import Form, FormError, SelectField, TextField
from wybra.forms.phone_contact import PhoneContactControl, UrlForContext


class PhoneContactWidgetError(FormError):
    """Raised when phone-contact widget field mappings are invalid."""


@dataclass(frozen=True, slots=True)
class PhoneContactFieldMapping:
    country_field: str
    subdivision_field: str
    phone_field: str


def phone_contact_context(
    form: Form,
    *,
    subdivision_field: str,
    phone_field: str,
    phone_prefix: str,
    phone_contact_status: str | None,
    target_id: str,
    render_field: Callable[[Form, str], Markup],
) -> dict[str, Any]:
    return {
        "form": form,
        "subdivision_field": subdivision_field,
        "phone_field": phone_field,
        "phone_prefix": phone_prefix,
        "phone_prefix_id": phone_prefix_id(phone_field),
        "phone_contact_status": phone_contact_status,
        "target_id": target_id,
        "render_field": render_field,
    }


def validate_phone_contact_fields(form: Form, **field_names: str) -> None:
    missing = {
        role: field_name
        for role, field_name in field_names.items()
        if field_name not in form.fields
    }
    if missing:
        missing_fields = ", ".join(
            f"{role}={field_name!r}" for role, field_name in sorted(missing.items())
        )
        raise PhoneContactWidgetError(
            f"Phone contact widget references unknown form field(s): {missing_fields}."
        )
    expected_types = {
        "country_field": SelectField,
        "subdivision_field": SelectField,
        "phone_field": TextField,
    }
    for role, expected_type in expected_types.items():
        field_name = field_names.get(role)
        if field_name is None:
            continue
        field = form.fields[field_name]
        if not isinstance(field, expected_type):
            raise PhoneContactWidgetError(
                f"Phone contact widget field {field_name!r} must be "
                f"{expected_type.__name__}."
            )


def phone_prefix_id(phone_field: str) -> str:
    return f"{phone_field}_dial_prefix"


def resolve_phone_contact_mapping(
    *,
    control: PhoneContactControl | None,
    country_field: str | None,
    subdivision_field: str | None,
    phone_field: str | None,
) -> PhoneContactFieldMapping:
    if control is not None:
        country_field = country_field or control.country_field
        subdivision_field = subdivision_field or control.subdivision_field
        phone_field = phone_field or control.phone_field
    if country_field is None or subdivision_field is None or phone_field is None:
        raise PhoneContactWidgetError(
            "Phone contact widget requires country, subdivision, and phone fields "
            "or a PhoneContactControl."
        )
    return PhoneContactFieldMapping(
        country_field=country_field,
        subdivision_field=subdivision_field,
        phone_field=phone_field,
    )


def dependent_url(
    control: PhoneContactControl | None,
    *,
    url_context: UrlForContext | None,
) -> str:
    if control is None or url_context is None:
        return ""
    return control.dependent_fields_url(url_context)


def form_text_value(form: Form, field_name: str) -> str | None:
    result = form.field_results.get(field_name)
    if result is not None:
        if isinstance(result.value, str):
            return result.value
        if isinstance(result.raw_value, str):
            return result.raw_value
        return None
    value = form.values.get(field_name)
    return value if isinstance(value, str) else None


__all__ = (
    "PhoneContactFieldMapping",
    "PhoneContactWidgetError",
    "dependent_url",
    "form_text_value",
    "phone_contact_context",
    "phone_prefix_id",
    "resolve_phone_contact_mapping",
    "validate_phone_contact_fields",
)
