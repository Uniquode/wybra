from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup

from wybra.forms.fields import FileUploadField, Form, FormError
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
) -> Markup:
    return TemplateFormRenderer(templates, widgets=widgets).render_field(
        form,
        field_name,
        widget=widget,
    )


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
) -> Markup:
    return TemplateFormRenderer(templates, widgets=widgets).render_form(
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


def forms_rendering_context(
    templates: TemplateCapability,
    csrf: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if csrf is not None:
        validate_csrf_rendering_context(csrf)
    renderer = TemplateFormRenderer(templates)
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
    "TemplateFormRenderer",
    "UnknownWidgetError",
    "forms_rendering_context",
    "render_csrf_field",
    "render_field",
    "render_form",
    "validate_csrf_rendering_context",
)
