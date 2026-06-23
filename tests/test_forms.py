import asyncio
import json
import logging
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
    Form,
    FormsCapability,
    FormsSettings,
    HiddenField,
    MultiSelectField,
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
    forms_rendering_context,
    render_csrf_field,
    render_field,
    render_form,
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
    csrf_token = HiddenField(required=False)


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


def test_unknown_submitted_fields_can_be_reported() -> None:
    form = ExampleForm(unknown_fields="error")

    result = form.parse({"unknown": "value"})

    assert not result.is_valid
    assert result.unknown_fields == ("unknown",)
    assert "__form__" in result.errors


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

    assert '<form class="wybra-form" method="post" action="/profile">' in html
    assert 'name="csrf_token"' in html
    assert 'value="secure-token"' in html
    assert 'name="preferred_name"' in html
    assert "David" in html
    assert 'type="submit"' in html
    assert 'type="reset"' in html
    assert "data-wybra-form-cancel" in html


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
    assert '<form class="wybra-form" method="post" action="/save">' in form_html
    assert 'type="hidden"' in csrf_html
    assert 'value="secure-token"' in csrf_html


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
