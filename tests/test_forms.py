import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.core.resources import PackageResourceSource, first_existing_resource
from wybra.forms import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    CheckboxField,
    ChoiceField,
    DateField,
    DateTimeField,
    FieldHandler,
    FieldResult,
    FileUploadField,
    Form,
    FormsCapability,
    FormsSettings,
    HiddenField,
    MultiSelectField,
    PhoneContactControl,
    PhoneContactWidgetError,
    PositiveIntegerField,
    RadioField,
    SelectField,
    SliderField,
    SwitchField,
    TemplateFormRenderer,
    TextAreaField,
    TextField,
    TimeField,
    UnknownInitialFieldError,
    UnknownWidgetError,
    csrf_exempt,
    field_handler,
    form_control,
    forms_rendering_context,
    normalise_phone_contact,
    register_phone_contact_field_handlers,
    render_csrf_field,
    render_field,
    render_form,
    render_phone_contact,
    render_phone_contact_fields,
    request_csrf_response_finalisation,
    request_form_data,
    validate_csrf,
)
from wybra.forms.context import forms_context
from wybra.forms.csrf import CsrfProtector
from wybra.site import start
from wybra.template.capabilities import DefaultTemplateCapability
from wybra.template.context import TemplateContext
from wybra.tools.validate import validate_command
from wybra.tools.validation.core import ValidationResult

PRONOUN_CHOICES = {
    "she|her": "she/her",
    "he|him": "he/him",
}
COUNTRY_OPTIONS = {"AU": "Australia", "NZ": "New Zealand"}
CONTACT_METHOD_OPTIONS = {"email": "Email", "sms": "SMS"}
INTEREST_OPTIONS = {"forms": "Forms", "auth": "Auth"}
SUBDIVISION_OPTIONS = {"NSW": "New South Wales", "VIC": "Victoria"}


@dataclass(frozen=True, slots=True)
class UploadedFile:
    filename: str
    content_type: str = "application/octet-stream"


def csrf_request(
    *,
    method: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [
                (name.lower().encode("ascii"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
        },
        receive,
    )


class ExampleForm(Form):
    preferred_name = TextField(max_length=64)
    bio = TextAreaField(label="Biography", max_length=1024, required=False)
    age = PositiveIntegerField(required=False)
    birthday = DateField(required=False)
    meeting_time = TimeField(required=False)
    published_at = DateTimeField(required=False)
    pronouns = ChoiceField(
        choices=PRONOUN_CHOICES,
        required=False,
    )
    country = SelectField(required=False)
    interests = MultiSelectField(required=False)
    contact_method = RadioField(required=False)
    public_profile = CheckboxField(required=False)
    email_updates = SwitchField(required=False)
    priority = SliderField(min_value=1, max_value=5, required=False)
    attachment = FileUploadField(required=False)
    csrf_token = HiddenField(required=False)


class PhoneContactForm(Form):
    country = SelectField(required=False)
    region = SelectField(label="State or region", required=False)
    mobile = TextField(label="Phone number", required=False)
    phone_contact = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
        handlers=(
            field_handler(
                "/phone-contact/fields",
                name="phone-contact-fields",
                methods={"GET"},
            ),
        ),
    )


def _forms_templates() -> DefaultTemplateCapability:
    return DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.forms", directory="templates"),
        ),
    )


def test_form_discovers_class_variable_fields_with_names_and_labels() -> None:
    form = ExampleForm()

    assert tuple(form.fields) == (
        "preferred_name",
        "bio",
        "age",
        "birthday",
        "meeting_time",
        "published_at",
        "pronouns",
        "country",
        "interests",
        "contact_method",
        "public_profile",
        "email_updates",
        "priority",
        "attachment",
        "csrf_token",
    )
    assert form.fields["preferred_name"].name == "preferred_name"
    assert form.fields["preferred_name"].label == "Preferred name"
    assert form.fields["bio"].label == "Biography"


def test_form_values_override_defaults_for_rendering() -> None:
    form = ExampleForm(
        defaults={"preferred_name": "Default", "bio": "Default bio"},
        values={"preferred_name": "David"},
    )

    assert form.fields["preferred_name"].value == "David"
    assert form.fields["bio"].value == "Default bio"


@pytest.mark.parametrize(
    ("field_name", "raw_value", "parsed_value"),
    (
        ("preferred_name", "David", "David"),
        ("bio", "Plain text", "Plain text"),
        ("age", "42", 42),
        ("birthday", "2026-06-23", date(2026, 6, 23)),
        ("meeting_time", "09:30:00", time(9, 30)),
        (
            "published_at",
            "2026-06-23T09:30:00",
            datetime(2026, 6, 23, 9, 30),
        ),
        ("pronouns", "she|her", "she|her"),
        ("country", "AU", "AU"),
        ("contact_method", "email", "email"),
        ("public_profile", "on", True),
        ("email_updates", "true", True),
        ("priority", "3", 3),
        ("csrf_token", "token", "token"),
    ),
)
def test_form_parses_common_field_types(
    field_name: str,
    raw_value: object,
    parsed_value: object,
) -> None:
    form = ExampleForm(
        options={
            "country": COUNTRY_OPTIONS,
            "contact_method": CONTACT_METHOD_OPTIONS,
        },
    )
    data = {"preferred_name": "David"}
    data[field_name] = raw_value

    result = form.parse(data)

    assert result.is_valid
    assert result.values[field_name] == parsed_value
    assert result.fields[field_name].raw_value == raw_value


def test_form_parses_multiselect_values() -> None:
    form = ExampleForm(options={"interests": INTEREST_OPTIONS})

    result = form.parse({"preferred_name": "David", "interests": ["forms", "auth"]})

    assert result.is_valid
    assert result.values["interests"] == ("forms", "auth")


def test_form_rejects_invalid_multiselect_values() -> None:
    form = ExampleForm(options={"interests": INTEREST_OPTIONS})
    raw_interests = ["forms", "invalid"]

    result = form.parse({"preferred_name": "David", "interests": raw_interests})

    assert not result.is_valid
    assert result.errors["interests"] == ("Select a valid option.",)
    assert result.fields["interests"].raw_value == raw_interests


@pytest.mark.parametrize(
    ("raw_value", "expected_error"),
    (
        ("0", "Must be at least 1."),
        ("6", "Must be at most 5."),
    ),
)
def test_slider_field_priority_min_max_violations(
    raw_value: object,
    expected_error: str,
) -> None:
    form = ExampleForm()

    result = form.parse({"preferred_name": "David", "priority": raw_value})

    assert not result.is_valid
    assert result.errors["priority"] == (expected_error,)
    assert result.fields["priority"].raw_value == raw_value


def test_optional_checkbox_preserves_missing_value_as_none() -> None:
    class OptionalCheckboxForm(Form):
        accepted = CheckboxField(required=False)

    result = OptionalCheckboxForm().parse({})

    assert result.is_valid
    assert result.fields["accepted"].value is None
    assert "accepted" not in result.values


def test_required_checkbox_rejects_missing_value() -> None:
    class RequiredCheckboxForm(Form):
        accepted = CheckboxField()

    result = RequiredCheckboxForm().parse({})

    assert not result.is_valid
    assert result.errors["accepted"] == ("This field is required.",)
    assert result.fields["accepted"].raw_value is None


def test_checkbox_parses_explicit_boolean_values() -> None:
    class ExplicitCheckboxForm(Form):
        accepted = CheckboxField()

    false_result = ExplicitCheckboxForm().parse({"accepted": "false"})
    true_result = ExplicitCheckboxForm().parse({"accepted": "on"})

    assert false_result.is_valid
    assert false_result.values["accepted"] is False
    assert true_result.is_valid
    assert true_result.values["accepted"] is True


def test_form_reports_validation_errors_and_preserves_raw_values() -> None:
    form = ExampleForm()

    result = form.parse({"age": "-1", "pronouns": "invalid"})

    assert not result.is_valid
    assert result.fields["age"].raw_value == "-1"
    assert result.fields["pronouns"].raw_value == "invalid"
    assert result.errors["age"]
    assert result.errors["pronouns"]


@pytest.mark.parametrize(
    "raw_value",
    (
        "<script>alert(1)</script>",
        "</strong>",
        "<!-- hidden -->",
        "hidden --!> end",
        "<!doctype html>",
        "<?xml version='1.0'?>",
    ),
)
@pytest.mark.parametrize("field_name", ("name", "bio", "token"))
def test_text_like_fields_reject_markup_by_default(
    field_name: str,
    raw_value: str,
) -> None:
    class ProtectedTextForm(Form):
        name = TextField(required=False)
        bio = TextAreaField(required=False)
        token = HiddenField(required=False)

    result = ProtectedTextForm().parse({field_name: raw_value})

    assert not result.is_valid
    assert result.errors[field_name] == ("Enter plain text without HTML or markup.",)
    assert result.fields[field_name].raw_value == raw_value
    assert result.fields[field_name].value is None
    assert field_name not in result.values


@pytest.mark.parametrize(
    "raw_value",
    (
        "1 < 2",
        "2 > 1",
        "C<++",
        "Use < placeholder text > here",
    ),
)
def test_text_fields_accept_non_markup_angle_bracket_text(raw_value: str) -> None:
    class ProtectedTextForm(Form):
        name = TextField(required=False)

    result = ProtectedTextForm().parse({"name": raw_value})

    assert result.is_valid
    assert result.values["name"] == raw_value


@pytest.mark.parametrize(
    "raw_value",
    (
        "hello\x00world",
        "hello\x1fworld",
        "hello\x7fworld",
        "hello\x80world",
    ),
)
def test_text_fields_reject_unsafe_control_characters(raw_value: str) -> None:
    class ProtectedTextForm(Form):
        name = TextField(required=False)

    result = ProtectedTextForm().parse({"name": raw_value})

    assert not result.is_valid
    assert result.errors["name"] == ("Enter text without unsafe control characters.",)
    assert result.fields["name"].raw_value == raw_value
    assert result.fields["name"].value is None
    assert "name" not in result.values


def test_text_fields_allow_html_when_explicitly_enabled() -> None:
    class HtmlTextForm(Form):
        content = TextAreaField(allow_html=True, max_length=64)

    result = HtmlTextForm().parse({"content": "<p>Hello</p>"})

    assert result.is_valid
    assert result.values["content"] == "<p>Hello</p>"


def test_text_fields_that_allow_html_keep_length_validation() -> None:
    class HtmlTextForm(Form):
        content = TextField(allow_html=True, max_length=4)

    result = HtmlTextForm().parse({"content": "<tag>"})

    assert not result.is_valid
    assert result.errors["content"] == ("Must be 4 characters or fewer.",)


def test_parse_clears_stale_field_value_for_omitted_optional_field() -> None:
    class OptionalNameForm(Form):
        name = TextField(required=False)

    form = OptionalNameForm(values={"name": "Previous"})

    result = form.parse({})

    assert result.is_valid
    assert result.values == {}
    assert form.fields["name"].value is None
    assert form.fields["name"].raw_value is None


def test_parse_clears_stale_field_value_for_omitted_required_field() -> None:
    class RequiredNameForm(Form):
        name = TextField()

    form = RequiredNameForm(values={"name": "Previous"})

    result = form.parse({})

    assert not result.is_valid
    assert result.errors["name"] == ("This field is required.",)
    assert form.fields["name"].value is None
    assert form.fields["name"].raw_value is None


def test_unknown_submitted_fields_can_be_reported() -> None:
    form = ExampleForm(unknown_fields="error")

    result = form.parse({"unknown": "value"})

    assert not result.is_valid
    assert result.unknown_fields == ("unknown",)
    assert None in form.errors
    assert None in result.errors


def test_unknown_initial_field_values_raise_form_field_error() -> None:
    with pytest.raises(UnknownInitialFieldError, match="unknown"):
        ExampleForm(values={"unknown": "value"}, unknown_fields="error")


def test_disabled_fields_render_values_but_do_not_parse_submissions() -> None:
    class DisabledForm(Form):
        token = HiddenField(disabled=True)

    form = DisabledForm(values={"token": "existing"})

    result = form.parse({"token": "attacker"})

    assert result.is_valid
    assert result.values == {}
    assert form.fields["token"].value == "existing"


def test_form_validates_field_by_explicit_name() -> None:
    class ReservedNameForm(Form):
        preferred_name = TextField()

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if (
                field_name == "preferred_name"
                and self.values.get(field_name) == "admin"
            ):
                self.add_error(field_name, "This preferred name is reserved.")
                local = False
            return inherited and local

    form = ReservedNameForm()
    result = form.parse({"preferred_name": "admin"})

    assert not form.is_valid()
    assert not result.is_valid
    assert form.errors["preferred_name"] == ["This preferred name is reserved."]
    assert result.errors["preferred_name"] == ("This preferred name is reserved.",)
    assert form.fields["preferred_name"].raw_value == "admin"
    assert form.fields["preferred_name"].value == "admin"


def test_form_validation_preserves_super_errors_and_adds_local_errors() -> None:
    class ExtraValidationForm(Form):
        age = PositiveIntegerField()

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name == "age":
                self.add_error(field_name, "Local validation still ran.")
                local = False
            return inherited and local

    form = ExtraValidationForm()
    result = form.parse({"age": "-1"})

    assert not result.is_valid
    assert form.errors["age"] == [
        "Enter a positive integer.",
        "Local validation still ran.",
    ]
    assert result.errors["age"] == (
        "Enter a positive integer.",
        "Local validation still ran.",
    )


def test_repeated_direct_validation_does_not_duplicate_base_errors() -> None:
    class AgeForm(Form):
        age = PositiveIntegerField()

    form = AgeForm()
    result = form.parse({"age": "-1"})

    assert not result.is_valid
    assert form.errors["age"] == ["Enter a positive integer."]

    assert not form.validate("age")
    assert form.errors["age"] == ["Enter a positive integer."]
    assert form.result.errors["age"] == ("Enter a positive integer.",)


def test_direct_field_validation_syncs_field_and_result_errors() -> None:
    class BlockableNameForm(Form):
        name = TextField(required=False)

        def __init__(self) -> None:
            super().__init__()
            self.name_blocked = False

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name == "name" and self.name_blocked:
                self.add_error(field_name, "Name is blocked.")
                local = False
            return inherited and local

    form = BlockableNameForm()
    result = form.parse({"name": "available"})

    assert result.is_valid
    assert form.fields["name"].errors == ()
    assert form.result.errors == {}

    form.name_blocked = True

    assert not form.validate("name")
    assert form.errors["name"] == ["Name is blocked."]
    assert form.fields["name"].errors == ("Name is blocked.",)
    assert form.result.errors["name"] == ("Name is blocked.",)


def test_form_result_is_read_only() -> None:
    form = ExampleForm(values={"preferred_name": "David"})

    with pytest.raises(AttributeError):
        form.result = form.result


def test_form_result_fields_are_read_only() -> None:
    form = ExampleForm(values={"preferred_name": "David"})

    with pytest.raises(TypeError):
        form.result.fields["preferred_name"] = FieldResult(
            name="preferred_name",
            value="Changed",
        )


def test_form_level_validation_uses_none_error_key() -> None:
    class AgreementForm(Form):
        accepted = CheckboxField(required=False)

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name is None and self.values.get("accepted") is not True:
                self.add_error(None, "You must accept the agreement.")
                local = False
            return inherited and local

    form = AgreementForm()
    result = form.parse({"accepted": "false"})

    assert not form.is_valid()
    assert not result.is_valid
    assert form.errors[None] == ["You must accept the agreement."]
    assert result.errors[None] == ("You must accept the agreement.",)


def test_multiple_errors_on_one_field_are_preserved() -> None:
    class MultipleErrorForm(Form):
        code = TextField()

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name == "code":
                self.add_error(field_name, "First error.")
                self.add_error(field_name, "Second error.")
                local = False
            return inherited and local

    form = MultipleErrorForm()
    result = form.parse({"code": "x"})

    assert not result.is_valid
    assert form.errors["code"] == ["First error.", "Second error."]
    assert result.errors["code"] == ("First error.", "Second error.")


def test_text_field_rejects_binary_input_before_local_validation() -> None:
    class BinaryTextForm(Form):
        name = TextField()

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name == "name":
                self.add_error(field_name, "Local validation still ran.")
                local = False
            return inherited and local

    form = BinaryTextForm()
    result = form.parse({"name": b"\xff\xfe"})

    assert not result.is_valid
    assert form.errors["name"] == [
        "Enter a valid text value.",
        "Local validation still ran.",
    ]
    assert result.errors["name"] == (
        "Enter a valid text value.",
        "Local validation still ran.",
    )


def test_programmatically_created_form_uses_validation_path() -> None:
    class DynamicValidationForm(Form):
        pass

    DynamicValidationForm.status = TextField()

    def validate(self: Form, field_name: str | None = None) -> bool:
        inherited = super(DynamicValidationForm, self).validate(field_name)
        local = True
        if field_name == "status" and self.values.get("status") == "closed":
            self.add_error(field_name, "Status cannot be closed.")
            local = False
        return inherited and local

    DynamicValidationForm.validate = validate

    form = DynamicValidationForm()
    result = form.parse({"status": "closed"})

    assert not result.is_valid
    assert form.errors["status"] == ["Status cannot be closed."]


def test_file_upload_field_parses_submitted_file() -> None:
    upload = UploadedFile(filename="document.pdf", content_type="application/pdf")

    result = ExampleForm().parse({"preferred_name": "David", "attachment": upload})

    assert result.is_valid
    assert result.values["attachment"] is upload
    assert result.fields["attachment"].raw_value is upload


def test_optional_file_upload_can_be_omitted() -> None:
    result = ExampleForm().parse({"preferred_name": "David"})

    assert result.is_valid
    assert "attachment" not in result.values


def test_required_file_upload_rejects_omission() -> None:
    class RequiredUploadForm(Form):
        attachment = FileUploadField()

    form = RequiredUploadForm()
    result = form.parse({})

    assert not result.is_valid
    assert form.errors["attachment"] == ["This field is required."]


def test_required_file_upload_rejects_empty_filename() -> None:
    class RequiredUploadForm(Form):
        attachment = FileUploadField()

    upload = UploadedFile(filename="")
    form = RequiredUploadForm()
    result = form.parse({"attachment": upload})

    assert not result.is_valid
    assert form.errors["attachment"] == ["This field is required."]
    assert result.errors["attachment"] == ("This field is required.",)


def test_optional_file_upload_treats_empty_filename_as_omitted() -> None:
    class OptionalUploadForm(Form):
        attachment = FileUploadField(required=False)

    upload = UploadedFile(filename="")
    result = OptionalUploadForm().parse({"attachment": upload})

    assert result.is_valid
    assert "attachment" not in result.values


def test_field_renderer_outputs_labels_options_and_errors() -> None:
    form = ExampleForm()
    result = form.parse({"preferred_name": "", "pronouns": "she|her"})
    renderer = TemplateFormRenderer(_forms_templates())

    text_html = renderer.render_field(form, "preferred_name")
    choice_html = renderer.render_field(form, "pronouns")

    assert 'for="preferred_name"' in text_html
    assert "Preferred name" in text_html
    assert "wybra-form-field--error" in text_html
    assert "This field is required." in text_html
    assert '<option value="she|her" selected>' in choice_html
    assert "she/her" in choice_html
    assert result is form.result


def test_form_renderer_outputs_form_actions_and_csrf_hidden_field() -> None:
    form = ExampleForm(values={"preferred_name": "David"})
    renderer = TemplateFormRenderer(_forms_templates())

    html = renderer.render_form(
        form,
        action="/profile",
        method="post",
        csrf={"csrf_field_name": "csrf_token", "csrf_token": "secure-token"},
        actions=("submit", "clear", "cancel"),
    )

    assert (
        '<form class="wybra-form" method="post" action="/profile" '
        'enctype="multipart/form-data">'
    ) in html
    assert 'name="csrf_token"' in html
    assert 'value="secure-token"' in html
    assert 'name="preferred_name"' in html
    assert "David" in html
    assert 'type="submit"' in html
    assert 'type="reset"' in html
    assert "data-wybra-form-cancel" in html


def test_form_renderer_outputs_form_level_errors_and_upload_encoding() -> None:
    class UploadErrorForm(Form):
        attachment = FileUploadField()

        def validate(self, field_name: str | None = None) -> bool:
            inherited = super().validate(field_name)
            local = True
            if field_name is None:
                self.add_error(None, "Form-level upload problem.")
                local = False
            return inherited and local

    form = UploadErrorForm()
    form.parse({"attachment": UploadedFile(filename="document.pdf")})
    renderer = TemplateFormRenderer(_forms_templates())

    html = renderer.render_form(form)

    assert 'enctype="multipart/form-data"' in html
    assert "Form-level upload problem." in html
    assert 'type="file"' in html


def test_form_renderer_uses_upload_encoding_for_custom_file_widget() -> None:
    class CustomUploadWidgetForm(Form):
        attachment = FileUploadField(widget="custom-file")

    renderer = TemplateFormRenderer(
        _forms_templates(),
        widgets={"custom-file": "forms/widgets/file.html"},
    )

    html = renderer.render_form(CustomUploadWidgetForm())

    assert 'enctype="multipart/form-data"' in html
    assert 'type="file"' in html


def test_form_renderer_raises_clear_error_for_unknown_widget() -> None:
    class UnknownWidgetForm(Form):
        value = TextField(widget="missing-widget")

    renderer = TemplateFormRenderer(_forms_templates())

    with pytest.raises(UnknownWidgetError, match="missing-widget.*value"):
        renderer.render_field(UnknownWidgetForm(), "value")


def test_form_renderer_accepts_custom_widget_mapping() -> None:
    class CustomWidgetForm(Form):
        value = TextField(widget="custom-text")

    renderer = TemplateFormRenderer(
        _forms_templates(),
        widgets={"custom-text": "forms/widgets/text.html"},
    )

    html = renderer.render_field(CustomWidgetForm(values={"value": "custom"}), "value")

    assert 'name="value"' in html
    assert 'value="custom"' in html


def test_template_rendering_helpers_return_safe_html() -> None:
    form = ExampleForm(values={"preferred_name": "David"})
    templates = _forms_templates()

    field_html = render_field(templates, form, "preferred_name")
    form_html = render_form(templates, form, action="/save")
    csrf_html = render_csrf_field(
        templates,
        csrf={"csrf_field_name": "csrf_token", "csrf_token": "secure-token"},
    )

    assert 'name="preferred_name"' in field_html
    assert (
        '<form class="wybra-form" method="post" action="/save" '
        'enctype="multipart/form-data">'
    ) in form_html
    assert 'type="hidden"' in csrf_html
    assert 'value="secure-token"' in csrf_html


def test_phone_contact_renderer_outputs_mapped_fields_and_state() -> None:
    form = PhoneContactForm(
        options={
            "country": COUNTRY_OPTIONS,
            "region": SUBDIVISION_OPTIONS,
        },
        values={"country": "AU", "region": "VIC", "mobile": "0412345678"},
    )
    form.parse({"country": "AU", "region": "VIC", "mobile": "<script>"})
    renderer = TemplateFormRenderer(_forms_templates())

    html = renderer.render_phone_contact(
        form,
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
        dependent_url="/phone-fields",
        phone_prefix="🇦🇺 +61",
        phone_contact_status="Not verified",
        target_id="account-phone-fields",
    )

    assert 'class="wybra-form-section wybra-phone-contact"' in html
    assert 'name="country"' in html
    assert 'value="AU" selected' in html
    assert 'hx-get="/phone-fields"' in html
    assert 'hx-target="#account-phone-fields"' in html
    assert 'name="region"' in html
    assert ">Victoria<" in html
    assert 'name="mobile"' in html
    assert "Enter plain text without HTML or markup." in html
    assert "🇦🇺 +61</span>" in html
    assert ">Not verified<" in html


def test_phone_contact_fragment_preserves_disabled_fields_and_mapped_names() -> None:
    form = PhoneContactForm(
        options={"region": SUBDIVISION_OPTIONS},
        values={"mobile": "0412345678"},
    )
    form.fields["region"].disabled = True
    form.fields["mobile"].disabled = True
    renderer = TemplateFormRenderer(_forms_templates())

    html = renderer.render_phone_contact_fields(
        form,
        subdivision_field="region",
        phone_field="mobile",
        phone_prefix="🇦🇺 +61",
        target_id="account-phone-fields",
    )

    assert 'id="account-phone-fields"' in html
    assert 'name="region"' in html
    assert 'name="mobile"' in html
    assert html.count("disabled") >= 2
    assert "0412345678" in html
    assert "🇦🇺 +61</span>" in html


def test_phone_contact_rendering_helpers_return_safe_html() -> None:
    templates = _forms_templates()
    form = PhoneContactForm(options={"country": COUNTRY_OPTIONS})

    widget_html = render_phone_contact(
        templates,
        form,
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
    )
    fragment_html = render_phone_contact_fields(
        templates,
        form,
        subdivision_field="region",
        phone_field="mobile",
    )

    assert 'class="wybra-form-section wybra-phone-contact"' in widget_html
    assert 'class="wybra-phone-contact-fields"' in fragment_html


def test_phone_contact_renderer_rejects_unknown_field_mapping() -> None:
    renderer = TemplateFormRenderer(_forms_templates())
    form = PhoneContactForm()

    with pytest.raises(
        PhoneContactWidgetError,
        match="country_field='missing_country'",
    ):
        renderer.render_phone_contact(
            form,
            country_field="missing_country",
            subdivision_field="region",
            phone_field="mobile",
        )


def test_phone_contact_fragment_rejects_unknown_field_mapping() -> None:
    renderer = TemplateFormRenderer(_forms_templates())
    form = PhoneContactForm()

    with pytest.raises(PhoneContactWidgetError, match="phone_field='missing_mobile'"):
        renderer.render_phone_contact_fields(
            form,
            subdivision_field="region",
            phone_field="missing_mobile",
        )


def test_phone_contact_renderer_rejects_wrong_field_type() -> None:
    class WrongPhoneContactForm(Form):
        country = TextField(required=False)
        region = SelectField(required=False)
        mobile = TextField(required=False)

    renderer = TemplateFormRenderer(_forms_templates())

    with pytest.raises(PhoneContactWidgetError, match="country.*SelectField"):
        renderer.render_phone_contact(
            WrongPhoneContactForm(),
            country_field="country",
            subdivision_field="region",
            phone_field="mobile",
        )


def test_phone_contact_prefix_uses_template_driven_empty_class() -> None:
    renderer = TemplateFormRenderer(_forms_templates())
    form = PhoneContactForm()

    html = renderer.render_phone_contact_fields(
        form,
        subdivision_field="region",
        phone_field="mobile",
        phone_prefix="  ",
    )

    assert 'class="wybra-phone-contact-prefix is-empty"' in html
    assert 'id="mobile_dial_prefix"' in html


def test_phone_contact_control_sources_unfiltered_options() -> None:
    control = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
    )

    country_options = control.country_options()
    subdivision_options = control.subdivision_options("AU")

    assert country_options["AU"] == "Australia"
    assert country_options["NZ"] == "New Zealand"
    assert subdivision_options["AU-VIC"] == "Victoria"


def test_phone_contact_control_filters_options_and_rejects_filtered_country() -> None:
    control = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
        country_filter=lambda country: country.code == "AU",
    )
    form = PhoneContactForm(
        options={
            "country": control.country_options(),
            "region": control.subdivision_options("NZ"),
        },
    )
    form.parse({"country": "NZ", "mobile": "+64211234567"})

    validation = control.validate(form)

    assert control.country_options() == {"AU": "Australia"}
    assert not validation.is_valid
    assert "country" in form.errors
    assert "Choose a valid country." in form.errors["country"]


def test_phone_contact_control_filters_and_rejects_filtered_subdivision() -> None:
    control = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
        subdivision_filter=lambda subdivision, _country: subdivision.code == "AU-VIC",
    )
    form = PhoneContactForm(
        options={
            "country": control.country_options(),
            "region": control.subdivision_options("AU"),
        },
    )
    control.apply_state(form, "AU")
    form.parse({"country": "AU", "region": "AU-NSW", "mobile": "0412 345 678"})

    validation = control.validate(form)

    assert control.subdivision_options("AU") == {"AU-VIC": "Victoria"}
    assert not validation.is_valid
    assert "region" in form.errors


def test_phone_contact_control_validates_and_normalises_phone_number() -> None:
    control = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
    )
    form = PhoneContactForm(
        options={
            "country": control.country_options(),
            "region": control.subdivision_options("AU"),
        },
    )
    control.apply_state(form, "AU")
    form.parse({"country": "AU", "region": "AU-VIC", "mobile": "0412 345 678"})

    validation = control.validate(form)

    assert validation.is_valid
    assert validation.normalised is not None
    assert validation.normalised.country_code == "AU"
    assert validation.normalised.subdivision_code == "AU-VIC"
    assert validation.normalised.normalised_number == "+61412345678"
    assert (
        normalise_phone_contact(
            "0412 345 678",
            country_code="AU",
        ).normalised_number
        == "+61412345678"
    )


def test_phone_contact_control_rejects_invalid_phone_number() -> None:
    control = PhoneContactControl(
        country_field="country",
        subdivision_field="region",
        phone_field="mobile",
    )
    form = PhoneContactForm(
        options={
            "country": control.country_options(),
            "region": control.subdivision_options("AU"),
        },
    )
    control.apply_state(form, "AU")
    form.parse({"country": "AU", "region": "AU-VIC", "mobile": "not a phone"})

    validation = control.validate(form)

    assert not validation.is_valid
    assert form.errors["mobile"] == ["Phone contact number is invalid."]


def test_phone_contact_control_declares_default_htmx_field_handler() -> None:
    handler = PhoneContactForm.phone_contact.dependent_fields_handler()

    assert isinstance(handler, FieldHandler)
    assert handler.path == "/phone-contact/fields"
    assert handler.name == "phone-contact-fields"
    assert handler.methods == frozenset({"GET"})
    assert handler.htmx is True
    assert handler.include_in_schema is False


def test_form_control_discovers_declared_phone_contact_control() -> None:
    assert (
        form_control(PhoneContactForm, "phone_contact")
        is PhoneContactForm.phone_contact
    )


def test_phone_contact_handler_declaration_does_not_register_routes() -> None:
    app = FastAPI()

    PhoneContactForm().parse({"country": "AU"})

    assert [route.path for route in app.routes] == [
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    ]


def test_phone_contact_field_handler_registers_htmx_fragment_route() -> None:
    router = APIRouter()

    register_phone_contact_field_handlers(
        router,
        control=PhoneContactForm.phone_contact,
        form_factory=lambda request: PhoneContactForm(
            options={
                "country": PhoneContactForm.phone_contact.country_options(),
                "region": PhoneContactForm.phone_contact.subdivision_options(
                    request.query_params.get("country")
                ),
            },
            values={"country": request.query_params.get("country") or ""},
        ),
        templates=lambda _request: _forms_templates(),
        target_id="test-phone-fields",
    )
    app = FastAPI()
    app.include_router(router)
    route = router.routes[0]

    response = TestClient(app).get("/phone-contact/fields?country=AU")

    assert getattr(route, "include_in_schema", True) is False
    assert "GET" in getattr(route, "methods", set())
    assert response.status_code == 200
    assert 'id="test-phone-fields"' in response.text
    assert "Victoria" in response.text
    assert "🇦🇺 +61" in response.text


def test_phone_contact_renderer_resolves_control_handler_url() -> None:
    router = APIRouter()

    register_phone_contact_field_handlers(
        router,
        control=PhoneContactForm.phone_contact,
        form_factory=lambda _request: PhoneContactForm(),
        templates=lambda _request: _forms_templates(),
    )

    @router.get("/form")
    async def form_view(request: Request) -> PlainTextResponse:
        form = PhoneContactForm(
            options={
                "country": PhoneContactForm.phone_contact.country_options(),
            },
        )
        html = TemplateFormRenderer(
            _forms_templates(),
            url_context=request,
        ).render_phone_contact(form, control=PhoneContactForm.phone_contact)
        return PlainTextResponse(str(html))

    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/form")

    assert response.status_code == 200
    assert 'hx-get="http://testserver/phone-contact/fields"' in response.text


def test_phone_contact_renderer_scopes_duplicate_control_handler_names() -> None:
    class BillingPhoneContactForm(Form):
        billing_country = SelectField(required=False)
        billing_region = SelectField(label="State or region", required=False)
        billing_mobile = TextField(label="Phone number", required=False)
        phone_contact = PhoneContactControl(
            country_field="billing_country",
            subdivision_field="billing_region",
            phone_field="billing_mobile",
            handlers=(
                field_handler(
                    "/billing-phone-contact/fields",
                    name="phone-contact-fields",
                    methods={"GET"},
                ),
            ),
        )

    router = APIRouter()
    register_phone_contact_field_handlers(
        router,
        control=PhoneContactForm.phone_contact,
        form_factory=lambda _request: PhoneContactForm(),
        templates=lambda _request: _forms_templates(),
    )
    register_phone_contact_field_handlers(
        router,
        control=BillingPhoneContactForm.phone_contact,
        form_factory=lambda _request: BillingPhoneContactForm(),
        templates=lambda _request: _forms_templates(),
    )

    @router.get("/delivery")
    async def delivery_view(request: Request) -> PlainTextResponse:
        html = TemplateFormRenderer(
            _forms_templates(),
            url_context=request,
        ).render_phone_contact(
            PhoneContactForm(),
            control=PhoneContactForm.phone_contact,
        )
        return PlainTextResponse(str(html))

    @router.get("/billing")
    async def billing_view(request: Request) -> PlainTextResponse:
        html = TemplateFormRenderer(
            _forms_templates(),
            url_context=request,
        ).render_phone_contact(
            BillingPhoneContactForm(),
            control=BillingPhoneContactForm.phone_contact,
        )
        return PlainTextResponse(str(html))

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    delivery_response = client.get("/delivery")
    billing_response = client.get("/billing")

    assert delivery_response.status_code == 200
    assert billing_response.status_code == 200
    assert 'hx-get="http://testserver/phone-contact/fields"' in delivery_response.text
    assert (
        'hx-get="http://testserver/billing-phone-contact/fields"'
        in billing_response.text
    )


def test_forms_rendering_context_rejects_incomplete_csrf_context() -> None:
    with pytest.raises(ValueError, match="csrf_token"):
        forms_rendering_context(
            _forms_templates(),
            csrf={"csrf_field_name": "csrf_token"},
        )


def test_forms_context_layers_valid_csrf_and_rendering_helpers() -> None:
    class SiteStub:
        def __init__(self) -> None:
            self._csrf = CsrfProtector("test-secret")
            self._templates = _forms_templates()

        def require_capability(self, capability_type: object) -> object:
            if capability_type is FormsCapability:
                return self._csrf
            return self._templates

    app = FastAPI()
    app.state.site = SiteStub()

    @app.get("/form")
    async def form_view(request: Request) -> dict[str, str]:
        context = forms_context(request, TemplateContext()).as_dict()
        csrf_html = context["render_csrf_field"]()
        return {
            "field_name": context["csrf_field_name"],
            "csrf_html": str(csrf_html),
        }

    response = TestClient(app).get("/form")

    assert response.status_code == 200
    assert response.json()["field_name"] == CSRF_FIELD_NAME
    assert 'name="csrf_token"' in response.json()["csrf_html"]


def test_forms_static_css_resource_is_available() -> None:
    resource = first_existing_resource(
        (PackageResourceSource(package="wybra.forms", directory="static"),),
        "styles/forms.css",
    )

    assert resource is not None


def test_forms_static_css_contains_phone_contact_widget_styles() -> None:
    resource = first_existing_resource(
        (PackageResourceSource(package="wybra.forms", directory="static"),),
        "styles/forms.css",
    )
    assert resource is not None
    css = resource.read_text(encoding="utf-8")

    assert ".wybra-phone-contact-control" in css
    assert ".wybra-phone-contact-prefix" in css
    assert ".wybra-phone-contact-status--unverified" in css


def test_csrf_form_validation_rejects_non_form_content_type() -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret")
    token = protector.create_token(nonce)
    body = json.dumps({CSRF_FIELD_NAME: token}).encode("utf-8")
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/json",
            "content-length": str(len(body)),
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )

    assert asyncio.run(protector.validate_request(request)) is False


def test_csrf_form_validation_rejects_oversized_form_body() -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret", max_form_body_bytes=8)
    token = protector.create_token(nonce)
    body = f"{CSRF_FIELD_NAME}={token}".encode()
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(len(body)),
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )

    assert asyncio.run(protector.validate_request(request)) is False


def test_csrf_form_validation_caches_parsed_form_for_downstream_views() -> None:
    async def assert_form_cache() -> None:
        nonce = "a" * 32
        protector = CsrfProtector("test-secret")
        token = protector.create_token(nonce)
        body = f"{CSRF_FIELD_NAME}={token}&field=value".encode()
        request = csrf_request(
            method="POST",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "content-length": str(len(body)),
                "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
            },
            body=body,
        )

        assert await protector.validate_request(request) is True
        form_data = await request_form_data(request)
        assert form_data.get(CSRF_FIELD_NAME) == token
        assert form_data.get("field") == "value"

    asyncio.run(assert_form_cache())


def test_csrf_form_validation_logs_rejection_reason(caplog) -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret")
    token = protector.create_token(nonce)
    body = f"{CSRF_FIELD_NAME}={token}".encode()
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )
    caplog.set_level(logging.DEBUG, logger="wybra.forms.csrf")

    assert asyncio.run(protector.validate_request(request)) is False
    assert "CSRF request rejected." in caplog.text
    assert any(
        getattr(record, "csrf_reason", None) == "missing_content_length"
        for record in caplog.records
    )


def test_csrf_dependency_allows_safe_methods_on_protected_router() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.get("/form")
    async def form() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).get("/form")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_csrf_dependency_raises_when_csrf_protector_misconfigured() -> None:
    app = FastAPI()
    app.state.csrf = object()
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/submit")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with pytest.raises(
        RuntimeError, match="CSRF protector is not configured correctly"
    ):
        TestClient(app).post("/submit", data={"field": "value"})


def test_csrf_dependency_rejects_unsafe_methods_without_token() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/form")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).post("/form", data={"field": "value"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token."}


def test_csrf_exempt_allows_route_to_bypass_protected_router() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/callback")
    @csrf_exempt
    async def callback() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).post("/callback", data={"field": "value"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_forms_settings_generates_local_secret(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wybra.forms.settings")

    settings = FormsSettings()

    assert settings.token_secret
    assert settings.cookie_secure is False
    assert "Generated startup-local CSRF token secret." in caplog.text


def test_forms_settings_load_settings_uses_config_service_sources() -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "deployment_environment": "production",
                        "modules": ("wybra.forms",),
                    },
                    "wybra.forms": {
                        "csrf_token_secret": "production-csrf-secret",
                        "csrf_cookie_secure": "true",
                    },
                }
            )
        ],
    )

    settings = FormsSettings.load_settings(config)

    assert settings.deployment_environment == "production"
    assert settings.token_secret == "production-csrf-secret"
    assert settings.cookie_secure is True


def test_forms_settings_load_settings_rejects_blank_token_secret() -> None:
    with pytest.raises(ConfigSourceError, match="csrf_token_secret"):
        ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {"modules": ("wybra.forms",)},
                        "wybra.forms": {"csrf_token_secret": "   "},
                    }
                )
            ],
        )


@pytest.mark.anyio
async def test_forms_setup_provides_forms_capability(tmp_path) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.forms",),
                },
            }
        ),
    )

    assert site.require_capability(FormsCapability)
    assert isinstance(app.state.csrf, CsrfProtector)


@pytest.mark.anyio
async def test_forms_setup_finalises_csrf_cookie_when_requested(tmp_path) -> None:
    app = FastAPI()

    @app.get("/form")
    async def form(request: Request) -> PlainTextResponse:
        request_csrf_response_finalisation(request)
        return PlainTextResponse("ok")

    @app.get("/partials/form")
    async def partial_form() -> PlainTextResponse:
        return PlainTextResponse("ok")

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.forms",),
                },
            }
        ),
    )

    with TestClient(app) as client:
        response = client.get("/form")
        partial_response = client.get("/partials/form")

    assert response.status_code == 200
    assert CSRF_COOKIE_NAME in response.cookies
    assert partial_response.status_code == 200
    assert CSRF_COOKIE_NAME not in partial_response.cookies


def test_validate_forms_target_is_available(monkeypatch, tmp_path) -> None:
    class Settings:
        modules = ("wybra.forms",)
        config = ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {
                            "config_path": tmp_path / "app.toml",
                            "project_root": tmp_path,
                            "modules": ("wybra.forms",),
                        },
                    }
                )
            ],
        )

    monkeypatch.setattr(
        "wybra.tools.validate._build_settings",
        lambda _overrides: Settings(),
    )

    assert validate_command.main(args=["forms"], standalone_mode=False) == 0


def test_validate_forms_reports_loaded_settings() -> None:
    from wybra.forms.validation import validate_forms

    result = validate_forms(
        type(
            "Settings",
            (),
            {
                "modules": ("wybra.forms",),
                "config": ConfigService(
                    [
                        MappingConfigSource(
                            {
                                "app": {"modules": ("wybra.forms",)},
                            }
                        )
                    ],
                ),
            },
        )()
    )

    assert isinstance(result, ValidationResult)
    assert result.is_ok
    assert any(
        check.description.startswith("forms settings load") for check in result.checks
    )
