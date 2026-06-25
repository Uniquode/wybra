from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup

from wybra.forms.fields import FileUploadField, Form, FormError
from wybra.forms.phone_contact import PhoneContactControl, UrlForContext
from wybra.forms.phone_contact_rendering import (
    PhoneContactWidgetError,
    form_text_value,
    phone_contact_context,
    resolve_phone_contact_mapping,
    validate_phone_contact_fields,
)
from wybra.forms.phone_contact_rendering import (
    dependent_url as resolve_dependent_url,
)
from wybra.template.capabilities import TemplateCapability

DEFAULT_FIELD_WIDGETS: dict[str, str] = {
    "text": "forms/widgets/text.html",
    "textarea": "forms/widgets/textarea.html",
    "number": "forms/widgets/text.html",
    "date": "forms/widgets/text.html",
    "time": "forms/widgets/text.html",
    "datetime": "forms/widgets/text.html",
    "select": "forms/widgets/select.html",
    "multiselect": "forms/widgets/select.html",
    "radio": "forms/widgets/options.html",
    "checkbox": "forms/widgets/checkbox.html",
    "switch": "forms/widgets/checkbox.html",
    "slider": "forms/widgets/text.html",
    "hidden": "forms/widgets/hidden.html",
    "file": "forms/widgets/file.html",
}

CSRF_RENDERING_CONTEXT_KEYS = frozenset(("csrf_field_name", "csrf_token"))


class UnknownWidgetError(FormError):
    """Raised when a form field references an unknown widget."""


@dataclass(frozen=True, slots=True)
class TemplateFormRenderer:
    templates: TemplateCapability
    widgets: Mapping[str, str] | None = None
    url_context: UrlForContext | None = None
    form_template: str = "forms/form.html"
    csrf_template: str = "forms/widgets/csrf.html"

    def render_field(
        self,
        form: Form,
        field_name: str,
        *,
        widget: str | None = None,
    ) -> Markup:
        field = form.fields[field_name]
        template_name = widget or self._widget_template(
            field.widget_name,
            field_name=field.name,
        )
        return _trusted_template_markup(
            self.templates.render_template(
                template_name,
                {"form": form, "field": field, "result": form.result},
            )
        )

    def render_form(
        self,
        form: Form,
        *,
        action: str = "",
        method: str = "post",
        enctype: str | None = None,
        csrf: Mapping[str, str] | None = None,
        actions: Sequence[str] = ("submit",),
    ) -> Markup:
        resolved_enctype = enctype or _default_enctype(form)
        fields = [
            self.render_field(form, name)
            for name, field in form.fields.items()
            if field.widget_name != "hidden"
        ]
        hidden_fields = [
            self.render_field(form, name)
            for name, field in form.fields.items()
            if field.widget_name == "hidden"
        ]
        csrf_field = self.render_csrf_field(csrf) if csrf else Markup("")
        form_errors = tuple(form.errors.get(None, ()))
        return _trusted_template_markup(
            self.templates.render_template(
                self.form_template,
                {
                    "form": form,
                    "action": action,
                    "method": method,
                    "enctype": resolved_enctype,
                    "fields": fields,
                    "hidden_fields": hidden_fields,
                    "csrf_field": csrf_field,
                    "actions": tuple(actions),
                    "form_errors": form_errors,
                },
            )
        )

    def render_csrf_field(self, csrf: Mapping[str, str]) -> Markup:
        validate_csrf_rendering_context(csrf)
        return _trusted_template_markup(
            self.templates.render_template(self.csrf_template, dict(csrf))
        )

    def render_phone_contact(
        self,
        form: Form,
        *,
        country_field: str | None = None,
        subdivision_field: str | None = None,
        phone_field: str | None = None,
        control: PhoneContactControl | None = None,
        dependent_url: str = "",
        phone_prefix: str = "",
        phone_contact_status: str | None = None,
        target_id: str = "wybra-phone-contact-fields",
    ) -> Markup:
        """Render a compound phone contact widget.

        Explicit field names override the fields declared by ``control``.
        ``dependent_url`` overrides any URL resolved from a control-declared
        handler. ``phone_prefix`` overrides the prefix derived from the control
        and current country value.
        """
        resolved = resolve_phone_contact_mapping(
            control=control,
            country_field=country_field,
            subdivision_field=subdivision_field,
            phone_field=phone_field,
        )
        dependent_url = dependent_url or resolve_dependent_url(
            control,
            url_context=self.url_context,
        )
        if not phone_prefix and control is not None:
            phone_prefix = control.phone_prefix(
                form_text_value(form, resolved.country_field)
            )
        validate_phone_contact_fields(
            form,
            country_field=resolved.country_field,
            subdivision_field=resolved.subdivision_field,
            phone_field=resolved.phone_field,
        )
        return _trusted_template_markup(
            self.templates.render_template(
                "forms/widgets/phone_contact.html",
                {
                    "country_field": resolved.country_field,
                    "dependent_url": dependent_url,
                }
                | phone_contact_context(
                    form,
                    subdivision_field=resolved.subdivision_field,
                    phone_field=resolved.phone_field,
                    phone_prefix=phone_prefix,
                    phone_contact_status=phone_contact_status,
                    target_id=target_id,
                    render_field=self.render_field,
                ),
            )
        )

    def render_phone_contact_fields(
        self,
        form: Form,
        *,
        subdivision_field: str,
        phone_field: str,
        phone_prefix: str = "",
        phone_contact_status: str | None = None,
        target_id: str = "wybra-phone-contact-fields",
    ) -> Markup:
        validate_phone_contact_fields(
            form,
            subdivision_field=subdivision_field,
            phone_field=phone_field,
        )
        return _trusted_template_markup(
            self.templates.render_template(
                "forms/widgets/phone_contact_fields.html",
                phone_contact_context(
                    form,
                    subdivision_field=subdivision_field,
                    phone_field=phone_field,
                    phone_prefix=phone_prefix,
                    phone_contact_status=phone_contact_status,
                    target_id=target_id,
                    render_field=self.render_field,
                ),
            )
        )

    def _widget_template(self, widget_name: str, *, field_name: str) -> str:
        widgets = self.widgets or {}
        template_name = widgets.get(widget_name) or DEFAULT_FIELD_WIDGETS.get(
            widget_name
        )
        if template_name is not None:
            return template_name
        known_widgets = sorted(set(DEFAULT_FIELD_WIDGETS) | set(widgets))
        raise UnknownWidgetError(
            f"Unknown form widget {widget_name!r} for field {field_name!r}. "
            f"Known widgets: {', '.join(known_widgets)}."
        )


def render_field(
    templates: TemplateCapability,
    form: Form,
    field_name: str,
    *,
    widget: str | None = None,
    widgets: Mapping[str, str] | None = None,
    url_context: UrlForContext | None = None,
) -> Markup:
    renderer = TemplateFormRenderer(
        templates,
        widgets=widgets,
        url_context=url_context,
    )
    return renderer.render_field(form, field_name, widget=widget)


def render_form(
    templates: TemplateCapability,
    form: Form,
    *,
    action: str = "",
    method: str = "post",
    enctype: str | None = None,
    csrf: Mapping[str, str] | None = None,
    actions: Sequence[str] = ("submit",),
    widgets: Mapping[str, str] | None = None,
    url_context: UrlForContext | None = None,
) -> Markup:
    renderer = TemplateFormRenderer(
        templates,
        widgets=widgets,
        url_context=url_context,
    )
    return renderer.render_form(
        form,
        action=action,
        method=method,
        enctype=enctype,
        csrf=csrf,
        actions=actions,
    )


def render_csrf_field(
    templates: TemplateCapability,
    *,
    csrf: Mapping[str, str],
) -> Markup:
    return TemplateFormRenderer(templates).render_csrf_field(csrf)


def render_phone_contact(
    templates: TemplateCapability,
    form: Form,
    *,
    country_field: str | None = None,
    subdivision_field: str | None = None,
    phone_field: str | None = None,
    control: PhoneContactControl | None = None,
    dependent_url: str = "",
    phone_prefix: str = "",
    phone_contact_status: str | None = None,
    target_id: str = "wybra-phone-contact-fields",
    widgets: Mapping[str, str] | None = None,
    url_context: UrlForContext | None = None,
) -> Markup:
    """Render a compound phone contact widget with renderer precedence rules.

    Explicit field names override the fields declared by ``control``.
    ``dependent_url`` overrides any URL resolved from a control-declared
    handler. ``phone_prefix`` overrides the prefix derived from the control and
    current country value.
    """
    renderer = TemplateFormRenderer(
        templates,
        widgets=widgets,
        url_context=url_context,
    )
    return renderer.render_phone_contact(
        form,
        country_field=country_field,
        subdivision_field=subdivision_field,
        phone_field=phone_field,
        control=control,
        dependent_url=dependent_url,
        phone_prefix=phone_prefix,
        phone_contact_status=phone_contact_status,
        target_id=target_id,
    )


def render_phone_contact_fields(
    templates: TemplateCapability,
    form: Form,
    *,
    subdivision_field: str,
    phone_field: str,
    phone_prefix: str = "",
    phone_contact_status: str | None = None,
    target_id: str = "wybra-phone-contact-fields",
    widgets: Mapping[str, str] | None = None,
    url_context: UrlForContext | None = None,
) -> Markup:
    renderer = TemplateFormRenderer(
        templates,
        widgets=widgets,
        url_context=url_context,
    )
    return renderer.render_phone_contact_fields(
        form,
        subdivision_field=subdivision_field,
        phone_field=phone_field,
        phone_prefix=phone_prefix,
        phone_contact_status=phone_contact_status,
        target_id=target_id,
    )


def forms_rendering_context(
    templates: TemplateCapability,
    csrf: Mapping[str, str] | None = None,
    url_context: UrlForContext | None = None,
) -> dict[str, Any]:
    if csrf is not None:
        validate_csrf_rendering_context(csrf)
    renderer = TemplateFormRenderer(templates, url_context=url_context)
    return {
        "render_form": lambda form, **kwargs: renderer.render_form(
            form,
            csrf=kwargs.pop("csrf", csrf),
            **kwargs,
        ),
        "render_field": renderer.render_field,
        "render_csrf_field": lambda **kwargs: renderer.render_csrf_field(
            kwargs or csrf or {}
        ),
        "render_phone_contact": renderer.render_phone_contact,
        "render_phone_contact_fields": renderer.render_phone_contact_fields,
    }


def validate_csrf_rendering_context(csrf: Mapping[str, str]) -> None:
    missing_keys = CSRF_RENDERING_CONTEXT_KEYS.difference(csrf)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"CSRF rendering context is missing required keys: {missing}")


def _trusted_template_markup(html: str) -> Markup:
    return Markup(html)  # nosec B704 - rendered by Jinja with autoescaping enabled.


def _default_enctype(form: Form) -> str | None:
    if any(isinstance(field, FileUploadField) for field in form.fields.values()):
        return "multipart/form-data"
    return None


__all__ = (
    "DEFAULT_FIELD_WIDGETS",
    "CSRF_RENDERING_CONTEXT_KEYS",
    "PhoneContactWidgetError",
    "TemplateFormRenderer",
    "UnknownWidgetError",
    "forms_rendering_context",
    "render_csrf_field",
    "render_field",
    "render_form",
    "render_phone_contact",
    "render_phone_contact_fields",
    "validate_csrf_rendering_context",
)
