import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import PlainTextResponse
from tests_support.form_binding.models import (
    FormAddress,
    FormContact,
    FormDocument,
    FormLabel,
    FormPhone,
    FormVersionedRecord,
)
from tortoise.exceptions import IntegrityError
from tortoise.models import Model

from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.core.resources import PackageResourceSource, first_existing_resource
from wybra.core.runtime import normalise_deployment_environment
from wybra.db.capabilities import DatabaseCapabilityError
from wybra.db.routing import DatabaseRouteInstance, DatabaseRouteRegistry
from wybra.db.versioning import (
    PositiveIntField,
    VersionField,
    VersionFieldError,
    version_field_check_constraint,
)
from wybra.forms import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    CSRF_TOKEN_SECRET_KEY_CURRENT,
    CSRF_TOKEN_SECRET_KEY_PREVIOUS,
    Attr,
    CheckboxField,
    ChoiceField,
    CompositeForm,
    DateField,
    DateTimeField,
    FieldHandler,
    FieldResult,
    FileUploadField,
    Form,
    FormError,
    FormFieldOptions,
    FormPostHandler,
    FormsCapability,
    FormsSettings,
    HiddenField,
    JsonPath,
    ModelBindingError,
    ModelForm,
    ModelFormDeclarationError,
    MultiSelectField,
    NonNegativeIntegerField,
    PhoneContactControl,
    PhoneContactError,
    PhoneContactWidgetError,
    PositiveIntegerField,
    RadioField,
    ReadOnly,
    RelationPage,
    RelationQueryContext,
    SaveResult,
    SelectField,
    SliderField,
    SwitchField,
    TemplateFormRenderer,
    TextAreaField,
    TextField,
    TimeField,
    UnknownInitialFieldError,
    UnknownWidgetError,
    field_handler,
    form_control,
    forms_rendering_context,
    model_of,
    normalise_phone_contact,
    register_phone_contact_field_handlers,
    render_csrf_field,
    render_field,
    render_form,
    render_phone_contact,
    render_phone_contact_fields,
    request_csrf_response_finalisation,
)
from wybra.forms.context import forms_context
from wybra.forms.csrf import CsrfProtector
from wybra.forms.setup import setup_site as setup_forms_site
from wybra.media.models import MediaItem, MediaResourceKey
from wybra.messages import (
    AlertRecord,
    DefaultMessagesCapability,
    MessagesCapability,
    MessagesSettings,
)
from wybra.services.secrets import (
    MissingSecretError,
    SecretsCapability,
    SecretsError,
    SecretValue,
)
from wybra.sessions.models import SessionRecordModel
from wybra.site import Site, start
from wybra.template.capabilities import DefaultTemplateCapability
from wybra.template.context import TemplateContext
from wybra.testing import WybraTestClient, migrated_test_database
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


@dataclass(slots=True)
class ExampleRecord:
    preferred_name: str = ""
    bio: str = ""
    website_links: dict[str, object] | None = None
    created_by: str = "system"
    disabled_value: str = "stored"


@dataclass(slots=True)
class PersistenceProbeRecord(ExampleRecord):
    commit_called: bool = False
    flush_called: bool = False
    rollback_called: bool = False
    add_called: bool = False

    def commit(self) -> None:
        self.commit_called = True

    def flush(self) -> None:
        self.flush_called = True

    def rollback(self) -> None:
        self.rollback_called = True

    def add(self, _record: object) -> None:
        self.add_called = True


class FakeSecretsCapability:
    def __init__(self, values: Mapping[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})

    def resolve(self, source: str, key: str) -> SecretValue:
        try:
            value = self.values[(source, key)]
        except KeyError as exc:
            raise MissingSecretError(source=source, key=key) from exc
        return SecretValue(value, source=source, key=key)

    def exists(self, source: str, key: str) -> bool:
        return (source, key) in self.values


class FailingPreviousCsrfSecretsCapability(FakeSecretsCapability):
    def resolve(self, source: str, key: str) -> SecretValue:
        if key == CSRF_TOKEN_SECRET_KEY_PREVIOUS:
            raise SecretsError("keychain unavailable")
        return super().resolve(source, key)


class RecordingMessagesStorage:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, object]] = []

    async def enqueue(
        self,
        request: Request,
        alert: AlertRecord,
    ) -> None:
        self.alerts.append((alert.severity, alert.message))

    async def peek(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        return ()

    async def acknowledge(self, request: Request, *, now: float) -> None:
        return None

    async def pop(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        return ()

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        return None

    async def cleanup(self, *, now: float) -> None:
        return None

    async def validate(self) -> None:
        return None


class RecordingMessagesCapability(DefaultMessagesCapability):
    def __init__(self) -> None:
        storage = RecordingMessagesStorage()
        super().__init__(
            settings=MessagesSettings.load_settings({"wybra.messages": {}}),
            storage=storage,
        )
        object.__setattr__(self, "_recording_storage", storage)

    @property
    def alerts(self) -> list[tuple[str, object]]:
        return self._recording_storage.alerts


def _forms_site(
    values: dict[str, dict[str, object]],
    *,
    deployment_environment: str = "local",
    environ: dict[str, str] | None = None,
) -> Site:
    if environ is not None:
        ConfigService.set_runtime_environment(environ)
    return Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource(values)],
            config_defs=(FormsSettings.module_config,),
            discover_module_config=False,
        ),
        deployment_environment=normalise_deployment_environment(deployment_environment),
    )


def form_post_request(
    messages: RecordingMessagesCapability | None = None,
) -> Request:
    app = FastAPI()
    site = Site(
        app=app,
        config=ConfigService([], discover_module_config=False),
    )
    app.state.site = site
    if messages is not None:
        site.provide_capability(MessagesCapability, messages)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/submit",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "app": app,
        },
        receive,
    )


class TestFormsTestDoubles:
    @pytest.mark.anyio
    async def test_recording_messages_capability_noop_methods_preserve_alerts(
        self,
    ) -> None:
        messages = RecordingMessagesCapability()
        request = form_post_request(messages)

        assert isinstance(messages, MessagesCapability)

        await messages.success(request, "Saved")
        await messages.error(request, "Failed")
        alerts = list(messages.alerts)

        assert await messages.peek_alerts(request) == ()
        renderable_alerts = await messages.renderable_alerts(request)
        assert tuple(renderable_alerts) == ()
        assert await messages.consume_alerts(request) == ()

        await messages.acknowledge_alerts(request)
        await messages.cleanup_session_data({})
        await messages.cleanup_expired(now=1.0)
        await messages.validate()

        assert messages.alerts == alerts


class ExampleModelForm(ModelForm):
    preferred_name = TextField(max_length=64)
    bio = TextAreaField(required=False)
    website = TextField(required=False)
    owner = TextField(required=False)
    disabled_value = TextField(disabled=True, required=False)

    class Meta:
        model = ExampleRecord
        bindings = {
            "website": JsonPath("website_links", "website"),
            "owner": ReadOnly(Attr("created_by")),
        }


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


@pytest.mark.anyio
class TestForms:
    async def test_form_discovers_class_variable_fields_with_names_and_labels(
        self,
    ) -> None:
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

    async def test_form_values_override_defaults_for_rendering(self) -> None:
        form = ExampleForm(
            defaults={"preferred_name": "Default", "bio": "Default bio"},
            values={"preferred_name": "David"},
        )

        assert form.fields["preferred_name"].value == "David"
        assert form.fields["bio"].value == "Default bio"

    async def test_form_saves_valid_values_to_explicit_object_target(self) -> None:
        @dataclass(slots=True)
        class Preferences:
            preferred_name: str = "Before"
            public_profile: bool = True

        class PreferencesForm(Form):
            preferred_name = TextField()
            public_profile = CheckboxField(required=False)

        target = Preferences()
        form = PreferencesForm(target=target)

        result = await form.parse({"preferred_name": "After"})
        saved = await form.save()

        assert result.is_valid
        assert isinstance(saved, SaveResult)
        assert saved.primary is target
        assert saved.original is not target
        assert saved.changed_fields == ("preferred_name", "public_profile")
        assert saved.updated
        assert saved.affected_count == 1
        assert target.preferred_name == "After"
        assert target.public_profile is False

    async def test_form_saves_valid_values_to_dictionary_without_target(self) -> None:
        class FilterForm(Form):
            query = TextField(required=False)
            include_archived = CheckboxField(required=False)

        form = FilterForm()

        await form.parse({"query": "security"})
        saved = await form.save()

        assert saved.primary == {"query": "security", "include_archived": False}
        assert saved.created
        assert saved.changed_fields == ("query", "include_archived")

    async def test_invalid_object_form_does_not_mutate_target(self) -> None:
        @dataclass(slots=True)
        class Preferences:
            preferred_name: str = "Before"

        class PreferencesForm(Form):
            preferred_name = TextField()

        target = Preferences()
        form = PreferencesForm(target=target)

        await form.parse({"preferred_name": "<script>"})

        with pytest.raises(FormError, match="invalid form"):
            await form.save()
        assert target.preferred_name == "Before"

    async def test_model_form_requires_meta_model(self) -> None:
        class MissingModelForm(ModelForm):
            name = TextField()

        with pytest.raises(ModelFormDeclarationError, match="Meta.model"):
            MissingModelForm()

    async def test_model_form_rejects_unknown_tortoise_model_field(self) -> None:
        class InvalidSessionForm(ModelForm):
            unknown = TextField()

            class Meta:
                model = SessionRecordModel

        with pytest.raises(ModelFormDeclarationError, match="Unknown Tortoise model"):
            InvalidSessionForm()

    async def test_model_form_generates_fields_from_explicit_tortoise_allowlist(
        self,
    ) -> None:
        class SessionForm(ModelForm):
            class Meta:
                model = SessionRecordModel
                fields = ("id", "data")

        form = SessionForm()

        assert isinstance(form.fields["id"], TextField)
        assert isinstance(form.fields["data"], TextAreaField)

    async def test_version_field_defaults_to_zero_and_rejects_negative_values(
        self,
    ) -> None:
        field = VersionField()

        assert isinstance(field, PositiveIntField)
        assert field.default == 0
        assert not field.null
        with pytest.raises(VersionFieldError, match="non-negative"):
            field.to_python_value(-1)

    async def test_version_field_check_constraint_has_a_stable_generated_name(
        self,
    ) -> None:
        constraint = version_field_check_constraint(FormVersionedRecord, "version")

        assert constraint.check == "version >= 0"
        assert (
            constraint.name
            == "test_form_versioned_record_version_non_negative_002fc68d"
        )

    async def test_version_field_database_constraint_rejects_raw_negative_values(
        self,
    ) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            record = await FormVersionedRecord.create(id=1, data="before")

            with pytest.raises(IntegrityError):
                await database.connection().execute_query(
                    "UPDATE test_form_versioned_record SET version = -1 WHERE id = ?",
                    [record.id],
                )

            assert (await FormVersionedRecord.get(id=record.id)).version == 0

    async def test_model_form_rejects_multiple_version_fields(self) -> None:
        class InvalidVersionedModel(Model):
            first_version = VersionField()
            second_version = VersionField()

        class InvalidVersionedForm(ModelForm):
            class Meta:
                model = InvalidVersionedModel

        with pytest.raises(ModelFormDeclarationError, match="multiple VersionField"):
            InvalidVersionedForm()

    async def test_model_form_detects_stale_versioned_update(self) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            record = await FormVersionedRecord.create(id=1, data="before")
            current = await FormVersionedRecord.get(id=record.id)
            stale_delete = await FormVersionedRecord.get(id=record.id)

            class VersionedForm(ModelForm):
                data = TextField()
                version = NonNegativeIntegerField(widget="hidden")

                class Meta:
                    model = FormVersionedRecord

            current_form = VersionedForm(
                instance=current,
                connection=database.capability().database(),
            )
            await current_form.parse({"data": "current", "version": "0"})
            current_result = await current_form.save()

            fresh = await FormVersionedRecord.get(id=record.id)
            stale_form = VersionedForm(
                instance=fresh,
                connection=database.capability().database(),
            )
            await stale_form.parse({"data": "stale", "version": "0"})
            stale_result = await stale_form.save()
            stale_delete_form = VersionedForm(
                instance=stale_delete,
                connection=database.capability().database(),
            )
            await stale_delete_form.parse({"data": "before", "version": "0"})
            stale_delete_result = await stale_delete_form.delete()
            persisted = await FormVersionedRecord.get(id=record.id)

            assert current_result.updated
            assert current.version == 1
            assert stale_result.affected_count == 0
            assert stale_form.result.errors[None] == (
                "This record was changed by another user.",
            )
            assert stale_delete_result.affected_count == 0
            assert stale_delete_form.result.errors[None] == (
                "This record was changed by another user.",
            )
            assert persisted.data == "current"
            assert persisted.version == 1

    async def test_generated_boolean_model_field_accepts_false(self) -> None:
        class EnabledForm(ModelForm):
            class Meta:
                model = FormVersionedRecord
                fields = ("enabled", "version")

        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            record = await FormVersionedRecord.create(id=1, data="before")
            form = EnabledForm(
                instance=record,
                connection=database.capability().database(),
            )

            result = await form.parse({"enabled": "false", "version": "0"})
            saved = await form.save()

            assert result.is_valid
            assert saved.updated
            assert not (await FormVersionedRecord.get(id=record.id)).enabled

    async def test_composite_form_updates_supplied_primary_without_replacing_members(
        self,
    ) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            ContactForm = type(
                "ContactForm",
                (CompositeForm,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {"models": (FormAddress, FormPhone, FormContact)},
                    )
                },
            )
            address = await FormAddress.create(id=1, street="Before")
            phone = await FormPhone.create(id=1, number="0400000000")
            contact = await FormContact.create(
                id=1,
                address=address,
                phone=phone,
                name="Ava",
            )
            form = ContactForm(
                connection=database.capability().database(),
                instances={None: contact},
            )
            await form.parse(
                {
                    "address__street": "After",
                    "phone__number": "0400000000",
                    "name": "Ava",
                }
            )

            saved = await form.save()

            assert saved.affected_count == 1
            assert await FormAddress.all().count() == 1
            assert await FormPhone.all().count() == 1
            assert (await FormAddress.get(id=address.id)).street == "After"

    async def test_composite_form_reports_stale_version_conflicts(self) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            VersionedCompositeForm = type(
                "VersionedCompositeForm",
                (CompositeForm,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {"models": (FormVersionedRecord,)},
                    )
                },
            )
            record = await FormVersionedRecord.create(id=1, data="before")
            current = await FormVersionedRecord.get(id=record.id)
            current_form = VersionedCompositeForm(
                connection=database.capability().database(),
                instances={None: current},
            )
            await current_form.parse({"data": "current", "version": "0"})
            await current_form.save()

            fresh = await FormVersionedRecord.get(id=record.id)
            stale_form = VersionedCompositeForm(
                connection=database.capability().database(),
                instances={None: fresh},
            )
            await stale_form.parse({"data": "stale", "version": "0"})

            saved = await stale_form.save()

            assert saved.affected_count == 0
            assert stale_form.result.errors[None] == (
                "This record was changed by another user.",
            )
            assert (await FormVersionedRecord.get(id=record.id)).data == "current"

    async def test_model_form_saves_many_to_many_multi_select(self) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            first = await FormLabel.create(id=1, name="First")
            second = await FormLabel.create(id=2, name="Second")
            document = await FormDocument.create(id=1)
            await document.labels.add(first)

            class DocumentForm(ModelForm):
                labels = MultiSelectField()

                class Meta:
                    model = FormDocument

            form = DocumentForm(
                instance=document,
                connection=database.capability().database(),
            )
            result = await form.parse({"labels": (str(first.id), str(second.id))})
            saved = await form.save()

            assert result.is_valid
            assert saved.changed_fields == ("labels",)
            assert [label.id for label in await document.labels.all()] == [1, 2]

            unchanged = DocumentForm(
                instance=document,
                connection=database.capability().database(),
            )
            await unchanged.parse({"labels": (str(first.id), str(second.id))})
            unchanged_saved = await unchanged.save()

            assert unchanged_saved.changed_fields == ()
            assert unchanged_saved.affected_count == 0

    async def test_composite_form_inferrs_fixed_related_members(self) -> None:
        async with migrated_test_database(
            modules=("tests_support.form_binding",)
        ) as database:
            ContactForm = type(
                "ContactForm",
                (CompositeForm,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {"models": (FormAddress, FormPhone, FormContact)},
                    )
                },
            )

            form = ContactForm(connection=database.capability().database())
            result = await form.parse(
                {
                    "address__street": "1 High Street",
                    "phone__number": "0400000000",
                    "name": "Ava",
                }
            )
            saved = await form.save()
            contact = await FormContact.get(id=saved.primary.id)

            assert result.is_valid
            assert tuple(form.fields) == ("address__street", "phone__number", "name")
            assert saved.affected_count == 3
            assert contact.name == "Ava"
            assert (await contact.address).street == "1 High Street"
            assert (await contact.phone).number == "0400000000"

            duplicate = ContactForm(connection=database.capability().database())
            await duplicate.parse(
                {
                    "address__street": "2 High Street",
                    "phone__number": "0400000001",
                    "name": "Ava",
                }
            )

            with pytest.raises(IntegrityError):
                await duplicate.save()
            assert await FormAddress.all().count() == 1
            assert await FormPhone.all().count() == 1

            invalid = ContactForm(connection=database.capability().database())
            invalid_result = await invalid.parse(
                {
                    "address__street": "3 High Street",
                    "phone__number": "0400000002",
                }
            )
            with pytest.raises(FormError, match="invalid form"):
                await invalid.save()
            assert not invalid_result.is_valid
            assert await FormAddress.all().count() == 1

            ExplicitContactForm = type(
                "ExplicitContactForm",
                (CompositeForm,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "models": (
                                model_of(FormContact, "address"),
                                model_of(FormContact, "phone"),
                                FormContact,
                            )
                        },
                    )
                },
            )
            explicit = ExplicitContactForm(connection=database.capability().database())

            assert tuple(explicit.fields) == (
                "address__street",
                "phone__number",
                "name",
            )

            OverriddenContactForm = type(
                "OverriddenContactForm",
                (CompositeForm,),
                {
                    "address__street": TextField(label="Origin street"),
                    "Meta": type(
                        "Meta",
                        (),
                        {"models": (FormAddress, FormPhone, FormContact)},
                    ),
                },
            )
            overridden = OverriddenContactForm(
                connection=database.capability().database()
            )
            assert overridden.fields["address__street"].label == "Origin street"

            CollectionContactForm = type(
                "CollectionContactForm",
                (CompositeForm,),
                {
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "models": (
                                model_of(FormDocument, "labels"),
                                FormDocument,
                            )
                        },
                    )
                },
            )
            with pytest.raises(ModelFormDeclarationError, match="fixed forward"):
                CollectionContactForm(connection=database.capability().database())

    async def test_model_form_rejects_unknown_binding_field(self) -> None:
        class UnknownBindingForm(ModelForm):
            name = TextField()

            class Meta:
                model = ExampleRecord
                bindings = {"missing": Attr("missing")}

        with pytest.raises(ModelFormDeclarationError, match="Unknown binding field"):
            UnknownBindingForm()

    async def test_model_form_meta_fields_and_options_control_editability(self) -> None:
        class RestrictedModelForm(ModelForm):
            preferred_name = TextField()
            bio = TextAreaField(required=False)

            class Meta:
                model = ExampleRecord
                fields = ("preferred_name",)
                form_options = {"preferred_name": FormFieldOptions(editable=False)}

        record = ExampleRecord(preferred_name="Before", bio="Preserved")
        form = RestrictedModelForm(instance=record)

        result = await form.parse({"preferred_name": "After", "bio": "Changed"})
        form.apply()

        assert tuple(form.fields) == ("preferred_name",)
        assert result.is_valid
        assert record.preferred_name == "Before"
        assert record.bio == "Preserved"

    async def test_model_form_persists_tortoise_model_through_connection(self) -> None:
        class SessionForm(ModelForm):
            id = TextField()
            data = TextField()
            created_at = TextField()
            updated_at = TextField()
            expires_at = TextField()

            class Meta:
                model = SessionRecordModel

        async with migrated_test_database(modules=("wybra.db",)) as database:
            form = SessionForm(connection=database.capability().database())
            await form.parse(
                {
                    "id": "form-session",
                    "data": "{}",
                    "created_at": "1.0",
                    "updated_at": "1.0",
                    "expires_at": "2.0",
                }
            )

            saved = await form.save()

            assert saved.primary.id == "form-session"
            assert saved.created
            assert saved.affected_count == 1
            assert await SessionRecordModel.filter(id="form-session").exists()

    async def test_model_form_requires_capability_for_tortoise_persistence(
        self,
    ) -> None:
        class SessionForm(ModelForm):
            id = TextField()
            data = TextField()
            created_at = TextField()
            updated_at = TextField()
            expires_at = TextField()

            class Meta:
                model = SessionRecordModel

        registry = DatabaseRouteRegistry(
            (
                DatabaseRouteInstance(
                    name="default",
                    alias="default",
                    roles=frozenset({"default", "writer"}),
                ),
            )
        )
        form = SessionForm(connection=registry.connection())
        await form.parse(
            {
                "id": "route-only-session",
                "data": "{}",
                "created_at": "1.0",
                "updated_at": "1.0",
                "expires_at": "2.0",
            }
        )

        with pytest.raises(
            DatabaseCapabilityError,
            match="no resolvable database capability",
        ):
            await form.save()

    async def test_model_form_updates_tortoise_model_through_connection(self) -> None:
        class SessionForm(ModelForm):
            data = TextField()

            class Meta:
                model = SessionRecordModel

        async with migrated_test_database(modules=("wybra.db",)) as database:
            record = await SessionRecordModel.create(
                id="updated-session",
                data='{"before": true}',
                created_at=1.0,
                updated_at=1.0,
                expires_at=2.0,
            )
            form = SessionForm(
                instance=record,
                connection=database.capability().database(),
            )
            await form.parse({"data": '{"after": true}'})

            saved = await form.save()

            assert saved.primary is record
            assert saved.original is not record
            assert saved.changed_fields == ("data",)
            assert saved.updated
            assert (await SessionRecordModel.get(id="updated-session")).data == (
                '{"after": true}'
            )

    async def test_model_form_skips_noop_existing_model_update(self) -> None:
        class MediaForm(ModelForm):
            category = TextField()

            class Meta:
                model = MediaItem

        async with migrated_test_database(modules=("wybra.media",)) as database:
            media = await MediaItem.create(
                category="document",
                storage_key="unchanged",
                size=1,
            )
            original_modified_at = media.modified_at
            form = MediaForm(
                instance=media,
                connection=database.capability().database(),
            )
            await form.parse({"category": "document"})

            saved = await form.save()

            assert saved.primary is media
            assert saved.original is not media
            assert saved.changed_fields == ()
            assert not saved.updated
            assert saved.affected_count == 0
            persisted = await MediaItem.get(id=media.id)
            assert persisted.modified_at == original_modified_at

    async def test_model_form_resolves_relation_through_writer_connection(self) -> None:
        class MediaResourceForm(ModelForm):
            media = SelectField()

            class Meta:
                model = MediaResourceKey

        async with migrated_test_database(modules=("wybra.media",)) as database:
            first = await MediaItem.create(
                category="document",
                storage_key="first",
                size=1,
            )
            second = await MediaItem.create(
                category="document",
                storage_key="second",
                size=1,
            )
            resource = await MediaResourceKey.create(
                resource_key="resource",
                media=first,
            )
            form = MediaResourceForm(
                instance=resource,
                connection=database.capability().database(),
            )

            await form.prepare_relations()
            result = await form.parse({"media": str(second.id)})
            saved = await form.save()

            assert result.is_valid
            assert form.fields["media"].options()
            assert saved.primary is resource
            persisted = await MediaResourceKey.get(resource_key="resource")
            assert persisted.media_id == second.id

    async def test_model_form_uses_configured_async_relation_callables(self) -> None:
        calls: list[str] = []

        async with migrated_test_database(modules=("wybra.media",)) as database:
            media = await MediaItem.create(
                category="document",
                storage_key="configured",
                size=1,
            )
            resource = await MediaResourceKey.create(
                resource_key="configured-resource",
                media=media,
            )

            async def query(context: RelationQueryContext) -> RelationPage:
                assert context.model is MediaItem
                calls.append("query")
                return RelationPage((media,))

            async def value(
                raw_value: object, context: RelationQueryContext
            ) -> object | None:
                assert context.model is MediaItem
                calls.append(f"value:{raw_value}")
                return media if raw_value == str(media.id) else None

            async def format_option(
                record: object, context: RelationQueryContext
            ) -> str:
                assert context.model is MediaItem
                assert record is media
                calls.append("format")
                return "Configured media"

            class MediaResourceForm(ModelForm):
                media = SelectField()

                class Meta:
                    model = MediaResourceKey
                    form_options = {
                        "media": FormFieldOptions(
                            relation_query=query,
                            relation_value=value,
                            option_format=format_option,
                        )
                    }

            form = MediaResourceForm(
                instance=resource,
                connection=database.capability().database(),
            )

            result = await form.parse({"media": str(media.id)})

            assert result.is_valid
            assert form.fields["media"].options()[0].label == "Configured media"
            assert calls == ["query", "format", "value:" + str(media.id)]

    async def test_model_form_delete_physically_removes_bound_instance(self) -> None:
        class MediaResourceForm(ModelForm):
            class Meta:
                model = MediaResourceKey

        async with migrated_test_database(modules=("wybra.media",)) as database:
            media = await MediaItem.create(
                category="document",
                storage_key="delete-target",
                size=1,
            )
            resource = await MediaResourceKey.create(
                resource_key="delete-target",
                media=media,
            )
            form = MediaResourceForm(
                instance=resource,
                connection=database.capability().database(),
            )

            deleted = await form.delete()

            assert deleted.primary is resource
            assert deleted.deleted
            assert deleted.affected_count == 1
            assert not await MediaResourceKey.filter(
                resource_key="delete-target"
            ).exists()

    async def test_model_form_delete_can_save_soft_deleted_instance(self) -> None:
        class SoftDeleteMediaForm(ModelForm):
            class Meta:
                model = MediaItem

            async def deletion_action(self, instance: MediaItem) -> str:
                instance.category = "deleted"
                return "soft"

        async with migrated_test_database(modules=("wybra.media",)) as database:
            media = await MediaItem.create(
                category="document",
                storage_key="soft-delete-target",
                size=1,
            )
            form = SoftDeleteMediaForm(
                instance=media,
                connection=database.capability().database(),
            )

            deleted = await form.delete()

            assert deleted.primary is media
            assert deleted.deleted
            assert deleted.updated
            assert (await MediaItem.get(id=media.id)).category == "deleted"

    async def test_model_form_retains_one_explicit_writer_route(self) -> None:
        registry = DatabaseRouteRegistry(
            (
                DatabaseRouteInstance(
                    name="default",
                    alias="reader",
                    roles=frozenset({"reader"}),
                ),
                DatabaseRouteInstance(
                    name="default",
                    alias="writer",
                    roles=frozenset({"default", "writer"}),
                ),
            )
        )
        connection = registry.connection()

        form = ExampleModelForm(connection=connection)

        assert form.connection is connection

    async def test_model_form_accepts_a_writer_route_falling_back_to_default(
        self,
    ) -> None:
        registry = DatabaseRouteRegistry(
            (
                DatabaseRouteInstance(
                    name="default",
                    alias="default",
                    roles=frozenset({"default"}),
                ),
            )
        )
        connection = registry.connection()

        form = ExampleModelForm(connection=connection)

        assert form.connection is connection

    async def test_model_form_selects_a_writer_route_from_connection(self) -> None:
        registry = DatabaseRouteRegistry(
            (
                DatabaseRouteInstance(
                    name="default",
                    alias="reader",
                    roles=frozenset({"reader"}),
                ),
                DatabaseRouteInstance(
                    name="default",
                    alias="writer",
                    roles=frozenset({"default", "writer"}),
                ),
            )
        )

        form = ExampleModelForm(connection=registry.connection())

        assert form.connection is not None

    async def test_model_form_renders_and_mutates_with_its_writer_route(self) -> None:
        registry = DatabaseRouteRegistry(
            (
                DatabaseRouteInstance(
                    name="default",
                    alias="reader",
                    roles=frozenset({"reader"}),
                ),
                DatabaseRouteInstance(
                    name="default",
                    alias="writer",
                    roles=frozenset({"default", "writer"}),
                ),
            )
        )
        reader_snapshot = ExampleRecord(preferred_name="Replica value")
        writer_record = ExampleRecord(preferred_name="Writer value")
        form = ExampleModelForm(
            instance=writer_record,
            connection=registry.connection(),
        )

        assert form.fields["preferred_name"].value == "Writer value"

        result = await form.parse({"preferred_name": "Updated writer value"})

        assert result.is_valid
        assert form.apply() is writer_record
        assert writer_record.preferred_name == "Updated writer value"
        assert reader_snapshot.preferred_name == "Replica value"

    async def test_model_form_loads_instance_values_with_explicit_value_precedence(
        self,
    ) -> None:
        record = ExampleRecord(
            preferred_name="Model",
            bio="Model bio",
            website_links={"website": "https://example.test", "other": "preserved"},
        )

        form = ExampleModelForm(
            instance=record,
            defaults={"preferred_name": "Default", "bio": "Default bio"},
            values={"preferred_name": "Submitted"},
        )

        assert form.fields["preferred_name"].value == "Submitted"
        assert form.fields["bio"].value == "Model bio"
        assert form.fields["website"].value == "https://example.test"
        assert form.fields["owner"].value == "system"

    async def test_model_form_applies_valid_values_to_existing_instance(self) -> None:
        record = ExampleRecord(
            preferred_name="Before",
            bio="Before bio",
            website_links={"website": "https://old.example", "other": "preserved"},
        )
        form = ExampleModelForm(instance=record)

        result = await form.parse(
            {
                "preferred_name": "After",
                "bio": "After bio",
                "website": "https://new.example",
                "owner": "attacker",
                "disabled_value": "submitted",
            }
        )
        applied = form.apply()

        assert result.is_valid
        assert applied is record
        assert record.preferred_name == "After"
        assert record.bio == "After bio"
        assert record.website_links == {
            "website": "https://new.example",
            "other": "preserved",
        }
        assert record.created_by == "system"
        assert record.disabled_value == "stored"

    async def test_model_form_applies_valid_values_to_new_instance_for_create_flow(
        self,
    ) -> None:
        record = ExampleRecord()
        form = ExampleModelForm()

        await form.parse(
            {
                "preferred_name": "Created",
                "bio": "Created bio",
                "website": "https://created.example",
            }
        )

        assert form.is_valid()
        assert form.apply(record) is record
        assert record.preferred_name == "Created"
        assert record.bio == "Created bio"
        assert record.website_links == {"website": "https://created.example"}

    async def test_model_form_does_not_write_invalid_values(self) -> None:
        record = ExampleRecord(preferred_name="Before")
        form = ExampleModelForm(instance=record)

        result = await form.parse({"preferred_name": "<script>alert(1)</script>"})
        applied = form.apply()

        assert not result.is_valid
        assert applied is record
        assert record.preferred_name == "Before"

    async def test_model_form_missing_same_name_attribute_fails_clearly(self) -> None:
        @dataclass(slots=True)
        class PartialRecord:
            bio: str = ""

        class MissingAttributeForm(ModelForm):
            preferred_name = TextField()

            class Meta:
                model = PartialRecord

        with pytest.raises(ModelBindingError, match="preferred_name"):
            MissingAttributeForm(instance=PartialRecord())

    async def test_model_form_apply_requires_instance(self) -> None:
        form = ExampleModelForm()
        await form.parse({"preferred_name": "David"})

        with pytest.raises(ModelBindingError, match="instance"):
            form.apply()

    async def test_model_form_rejects_unsupported_binding_declarations(self) -> None:
        class UnsupportedBindingForm(ModelForm):
            name = TextField()

            class Meta:
                model = ExampleRecord
                bindings = {"name": object()}

        with pytest.raises(ModelFormDeclarationError, match="Unsupported binding"):
            UnsupportedBindingForm()

    async def test_model_form_does_not_manage_persistence_transactions(self) -> None:
        record = PersistenceProbeRecord()
        form = ExampleModelForm()
        await form.parse({"preferred_name": "David", "bio": "Bio"})

        form.apply(record)

        assert record.preferred_name == "David"
        assert not record.commit_called
        assert not record.flush_called
        assert not record.rollback_called
        assert not record.add_called

    async def test_model_form_remains_plain_object_compatible(self) -> None:
        class PlainRecord:
            preferred_name = ""

        class PlainModelForm(ModelForm):
            preferred_name = TextField()

            class Meta:
                model = PlainRecord

        record = PlainRecord()
        form = PlainModelForm()
        await form.parse({"preferred_name": "Plain"})

        form.apply(record)

        assert record.preferred_name == "Plain"

    @pytest.mark.anyio
    async def test_form_post_handler_adds_success_message_after_valid_commit(
        self,
    ) -> None:
        messages = RecordingMessagesCapability()

        class SavingPostHandler(FormPostHandler[ExampleForm]):
            success_message = "Saved"

            def __init__(self, form: ExampleForm) -> None:
                super().__init__(form)
                self.committed = False

            def commit(self, request: Request, form: ExampleForm) -> None:
                self.committed = True

        handler = SavingPostHandler(ExampleForm())

        result = await handler.handle(
            form_post_request(messages),
            {"preferred_name": "David"},
        )

        assert result.is_valid
        assert result.committed
        assert handler.committed
        assert messages.alerts == [("success", "Saved")]

    @pytest.mark.anyio
    async def test_form_post_handler_without_success_message_emits_no_alert(
        self,
    ) -> None:
        messages = RecordingMessagesCapability()

        class SilentPostHandler(FormPostHandler[ExampleForm]):
            def __init__(self, form: ExampleForm) -> None:
                super().__init__(form)
                self.committed = False

            def commit(self, request: Request, form: ExampleForm) -> None:
                self.committed = True

        handler = SilentPostHandler(ExampleForm())

        result = await handler.handle(
            form_post_request(messages),
            {"preferred_name": "David"},
        )

        assert result.is_valid
        assert result.committed
        assert handler.committed
        assert messages.alerts == []

    @pytest.mark.anyio
    async def test_form_post_handler_adds_failure_message_for_invalid_form(
        self,
    ) -> None:
        messages = RecordingMessagesCapability()

        class SavingPostHandler(FormPostHandler[ExampleForm]):
            failure_message = "Failed"

            def __init__(self, form: ExampleForm) -> None:
                super().__init__(form)
                self.committed = False

            def commit(self, request: Request, form: ExampleForm) -> None:
                self.committed = True

        handler = SavingPostHandler(ExampleForm())

        result = await handler.handle(
            form_post_request(messages),
            {"preferred_name": ""},
        )

        assert not result.is_valid
        assert not result.committed
        assert not handler.committed
        assert messages.alerts == [("error", "Failed")]

    @pytest.mark.anyio
    async def test_form_post_handler_adds_failure_message_when_commit_adds_error(
        self,
    ) -> None:
        messages = RecordingMessagesCapability()

        class SavingPostHandler(FormPostHandler[ExampleForm]):
            failure_message = "Failed"

            def commit(self, request: Request, form: ExampleForm) -> None:
                form.add_error(None, "Could not save.")

        handler = SavingPostHandler(ExampleForm())

        result = await handler.handle(
            form_post_request(messages),
            {"preferred_name": "David"},
        )

        assert not result.is_valid
        assert not result.committed
        assert result.result.errors[None] == ("Could not save.",)
        assert messages.alerts == [("error", "Failed")]

    @pytest.mark.anyio
    async def test_form_post_handler_allows_message_hooks(self) -> None:
        messages = RecordingMessagesCapability()

        class HookedPostHandler(FormPostHandler[ExampleForm]):
            def get_success_message(self) -> str | None:
                return f"Saved {self.form.values['preferred_name']}."

        handler = HookedPostHandler(ExampleForm())

        result = await handler.handle(
            form_post_request(messages),
            {"preferred_name": "David"},
        )

        assert result.committed
        assert messages.alerts == [("success", "Saved David.")]

    @pytest.mark.anyio
    async def test_form_post_handler_does_not_require_messages_capability(self) -> None:
        handler = FormPostHandler(
            ExampleForm(),
            success_message="Saved",
            failure_message="Failed",
        )

        result = await handler.handle(
            form_post_request(),
            {"preferred_name": "David"},
        )

        assert result.is_valid
        assert result.committed

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
    async def test_form_parses_common_field_types(
        self,
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

        result = await form.parse(data)

        assert result.is_valid
        assert result.values[field_name] == parsed_value
        assert result.fields[field_name].raw_value == raw_value

    async def test_form_parses_multiselect_values(self) -> None:
        form = ExampleForm(options={"interests": INTEREST_OPTIONS})

        result = await form.parse(
            {"preferred_name": "David", "interests": ["forms", "auth"]}
        )

        assert result.is_valid
        assert result.values["interests"] == ("forms", "auth")

    async def test_form_rejects_invalid_multiselect_values(self) -> None:
        form = ExampleForm(options={"interests": INTEREST_OPTIONS})
        raw_interests = ["forms", "invalid"]

        result = await form.parse(
            {"preferred_name": "David", "interests": raw_interests}
        )

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
    async def test_slider_field_priority_min_max_violations(
        self,
        raw_value: object,
        expected_error: str,
    ) -> None:
        form = ExampleForm()

        result = await form.parse({"preferred_name": "David", "priority": raw_value})

        assert not result.is_valid
        assert result.errors["priority"] == (expected_error,)
        assert result.fields["priority"].raw_value == raw_value

    async def test_optional_checkbox_normalises_missing_value_to_false(self) -> None:
        class OptionalCheckboxForm(Form):
            accepted = CheckboxField(required=False)

        result = await OptionalCheckboxForm().parse({})

        assert result.is_valid
        assert result.fields["accepted"].value is False
        assert result.values["accepted"] is False

    async def test_required_checkbox_rejects_missing_value(self) -> None:
        class RequiredCheckboxForm(Form):
            accepted = CheckboxField()

        result = await RequiredCheckboxForm().parse({})

        assert not result.is_valid
        assert result.errors["accepted"] == ("This field requires affirmation.",)
        assert result.fields["accepted"].raw_value is None

    async def test_checkbox_parses_explicit_boolean_values(self) -> None:
        class ExplicitCheckboxForm(Form):
            accepted = CheckboxField(required=False)

        false_result = await ExplicitCheckboxForm().parse({"accepted": "false"})
        true_result = await ExplicitCheckboxForm().parse({"accepted": "on"})

        assert false_result.is_valid
        assert false_result.values["accepted"] is False
        assert true_result.is_valid
        assert true_result.values["accepted"] is True

    async def test_form_reports_validation_errors_and_preserves_raw_values(
        self,
    ) -> None:
        form = ExampleForm()

        result = await form.parse({"age": "-1", "pronouns": "invalid"})

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
    async def test_text_like_fields_reject_markup_by_default(
        self,
        field_name: str,
        raw_value: str,
    ) -> None:
        class ProtectedTextForm(Form):
            name = TextField(required=False)
            bio = TextAreaField(required=False)
            token = HiddenField(required=False)

        result = await ProtectedTextForm().parse({field_name: raw_value})

        assert not result.is_valid
        assert result.errors[field_name] == (
            "Enter plain text without HTML or markup.",
        )
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
    async def test_text_fields_accept_non_markup_angle_bracket_text(
        self, raw_value: str
    ) -> None:
        class ProtectedTextForm(Form):
            name = TextField(required=False)

        result = await ProtectedTextForm().parse({"name": raw_value})

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
    async def test_text_fields_reject_unsafe_control_characters(
        self, raw_value: str
    ) -> None:
        class ProtectedTextForm(Form):
            name = TextField(required=False)

        result = await ProtectedTextForm().parse({"name": raw_value})

        assert not result.is_valid
        assert result.errors["name"] == (
            "Enter text without unsafe control characters.",
        )
        assert result.fields["name"].raw_value == raw_value
        assert result.fields["name"].value is None
        assert "name" not in result.values

    async def test_text_fields_allow_html_when_explicitly_enabled(self) -> None:
        class HtmlTextForm(Form):
            content = TextAreaField(allow_html=True, max_length=64)

        result = await HtmlTextForm().parse({"content": "<p>Hello</p>"})

        assert result.is_valid
        assert result.values["content"] == "<p>Hello</p>"

    async def test_text_fields_that_allow_html_keep_length_validation(self) -> None:
        class HtmlTextForm(Form):
            content = TextField(allow_html=True, max_length=4)

        result = await HtmlTextForm().parse({"content": "<tag>"})

        assert not result.is_valid
        assert result.errors["content"] == ("Must be 4 characters or fewer.",)

    async def test_parse_clears_stale_field_value_for_omitted_optional_field(
        self,
    ) -> None:
        class OptionalNameForm(Form):
            name = TextField(required=False)

        form = OptionalNameForm(values={"name": "Previous"})

        result = await form.parse({})

        assert result.is_valid
        assert result.values == {}
        assert form.fields["name"].value is None
        assert form.fields["name"].raw_value is None

    async def test_parse_clears_stale_field_value_for_omitted_required_field(
        self,
    ) -> None:
        class RequiredNameForm(Form):
            name = TextField()

        form = RequiredNameForm(values={"name": "Previous"})

        result = await form.parse({})

        assert not result.is_valid
        assert result.errors["name"] == ("This field is required.",)
        assert form.fields["name"].value is None
        assert form.fields["name"].raw_value is None

    async def test_unknown_submitted_fields_can_be_reported(self) -> None:
        form = ExampleForm(unknown_fields="error")

        result = await form.parse({"unknown": "value"})

        assert not result.is_valid
        assert result.unknown_fields == ("unknown",)
        assert None in form.errors
        assert None in result.errors

    async def test_unknown_initial_field_values_raise_form_field_error(self) -> None:
        with pytest.raises(UnknownInitialFieldError, match="unknown"):
            ExampleForm(values={"unknown": "value"}, unknown_fields="error")

    async def test_disabled_fields_render_values_but_do_not_parse_submissions(
        self,
    ) -> None:
        class DisabledForm(Form):
            token = HiddenField(disabled=True)

        form = DisabledForm(values={"token": "existing"})

        result = await form.parse({"token": "attacker"})

        assert result.is_valid
        assert result.values == {}
        assert form.fields["token"].value == "existing"

    async def test_form_validates_field_by_explicit_name(self) -> None:
        class ReservedNameForm(Form):
            preferred_name = TextField()

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if (
                    field_name == "preferred_name"
                    and self.values.get(field_name) == "admin"
                ):
                    self.add_error(field_name, "This preferred name is reserved.")
                    local = False
                return inherited and local

        form = ReservedNameForm()
        result = await form.parse({"preferred_name": "admin"})

        assert not form.is_valid()
        assert not result.is_valid
        assert form.errors["preferred_name"] == ["This preferred name is reserved."]
        assert result.errors["preferred_name"] == ("This preferred name is reserved.",)
        assert form.fields["preferred_name"].raw_value == "admin"
        assert form.fields["preferred_name"].value == "admin"

    async def test_form_validation_preserves_super_errors_and_adds_local_errors(
        self,
    ) -> None:
        class ExtraValidationForm(Form):
            age = PositiveIntegerField()

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name == "age":
                    self.add_error(field_name, "Local validation still ran.")
                    local = False
                return inherited and local

        form = ExtraValidationForm()
        result = await form.parse({"age": "-1"})

        assert not result.is_valid
        assert form.errors["age"] == [
            "Enter a positive integer.",
            "Local validation still ran.",
        ]
        assert result.errors["age"] == (
            "Enter a positive integer.",
            "Local validation still ran.",
        )

    async def test_repeated_direct_validation_does_not_duplicate_base_errors(
        self,
    ) -> None:
        class AgeForm(Form):
            age = PositiveIntegerField()

        form = AgeForm()
        result = await form.parse({"age": "-1"})

        assert not result.is_valid
        assert form.errors["age"] == ["Enter a positive integer."]

        assert not await form.validate("age")
        assert form.errors["age"] == ["Enter a positive integer."]
        assert form.result.errors["age"] == ("Enter a positive integer.",)

    async def test_direct_field_validation_syncs_field_and_result_errors(self) -> None:
        class BlockableNameForm(Form):
            name = TextField(required=False)

            def __init__(self) -> None:
                super().__init__()
                self.name_blocked = False

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name == "name" and self.name_blocked:
                    self.add_error(field_name, "Name is blocked.")
                    local = False
                return inherited and local

        form = BlockableNameForm()
        result = await form.parse({"name": "available"})

        assert result.is_valid
        assert form.fields["name"].errors == ()
        assert form.result.errors == {}

        form.name_blocked = True

        assert not await form.validate("name")
        assert form.errors["name"] == ["Name is blocked."]
        assert form.fields["name"].errors == ("Name is blocked.",)
        assert form.result.errors["name"] == ("Name is blocked.",)

    async def test_form_result_is_read_only(self) -> None:
        form = ExampleForm(values={"preferred_name": "David"})

        with pytest.raises(AttributeError):
            form.result = form.result

    async def test_form_result_fields_are_read_only(self) -> None:
        form = ExampleForm(values={"preferred_name": "David"})

        with pytest.raises(TypeError):
            form.result.fields["preferred_name"] = FieldResult(
                name="preferred_name",
                value="Changed",
            )

    async def test_form_level_validation_uses_none_error_key(self) -> None:
        class AgreementForm(Form):
            accepted = CheckboxField(required=False)

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name is None and self.values.get("accepted") is not True:
                    self.add_error(None, "You must accept the agreement.")
                    local = False
                return inherited and local

        form = AgreementForm()
        result = await form.parse({"accepted": "false"})

        assert not form.is_valid()
        assert not result.is_valid
        assert form.errors[None] == ["You must accept the agreement."]
        assert result.errors[None] == ("You must accept the agreement.",)

    async def test_multiple_errors_on_one_field_are_preserved(self) -> None:
        class MultipleErrorForm(Form):
            code = TextField()

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name == "code":
                    self.add_error(field_name, "First error.")
                    self.add_error(field_name, "Second error.")
                    local = False
                return inherited and local

        form = MultipleErrorForm()
        result = await form.parse({"code": "x"})

        assert not result.is_valid
        assert form.errors["code"] == ["First error.", "Second error."]
        assert result.errors["code"] == ("First error.", "Second error.")

    async def test_text_field_rejects_binary_input_before_local_validation(
        self,
    ) -> None:
        class BinaryTextForm(Form):
            name = TextField()

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name == "name":
                    self.add_error(field_name, "Local validation still ran.")
                    local = False
                return inherited and local

        form = BinaryTextForm()
        result = await form.parse({"name": b"\xff\xfe"})

        assert not result.is_valid
        assert form.errors["name"] == [
            "Enter a valid text value.",
            "Local validation still ran.",
        ]
        assert result.errors["name"] == (
            "Enter a valid text value.",
            "Local validation still ran.",
        )

    async def test_programmatically_created_form_uses_validation_path(self) -> None:
        class DynamicValidationForm(Form):
            pass

        DynamicValidationForm.status = TextField()

        async def validate(self: Form, field_name: str | None = None) -> bool:
            inherited = await super(DynamicValidationForm, self).validate(field_name)
            local = True
            if field_name == "status" and self.values.get("status") == "closed":
                self.add_error(field_name, "Status cannot be closed.")
                local = False
            return inherited and local

        DynamicValidationForm.validate = validate

        form = DynamicValidationForm()
        result = await form.parse({"status": "closed"})

        assert not result.is_valid
        assert form.errors["status"] == ["Status cannot be closed."]

    async def test_file_upload_field_parses_submitted_file(self) -> None:
        upload = UploadedFile(filename="document.pdf", content_type="application/pdf")

        result = await ExampleForm().parse(
            {"preferred_name": "David", "attachment": upload}
        )

        assert result.is_valid
        assert result.values["attachment"] is upload
        assert result.fields["attachment"].raw_value is upload

    async def test_optional_file_upload_can_be_omitted(self) -> None:
        result = await ExampleForm().parse({"preferred_name": "David"})

        assert result.is_valid
        assert "attachment" not in result.values

    async def test_required_file_upload_rejects_omission(self) -> None:
        class RequiredUploadForm(Form):
            attachment = FileUploadField()

        form = RequiredUploadForm()
        result = await form.parse({})

        assert not result.is_valid
        assert form.errors["attachment"] == ["This field is required."]

    async def test_required_file_upload_rejects_empty_filename(self) -> None:
        class RequiredUploadForm(Form):
            attachment = FileUploadField()

        upload = UploadedFile(filename="")
        form = RequiredUploadForm()
        result = await form.parse({"attachment": upload})

        assert not result.is_valid
        assert form.errors["attachment"] == ["This field is required."]
        assert result.errors["attachment"] == ("This field is required.",)

    async def test_optional_file_upload_treats_empty_filename_as_omitted(self) -> None:
        class OptionalUploadForm(Form):
            attachment = FileUploadField(required=False)

        upload = UploadedFile(filename="")
        result = await OptionalUploadForm().parse({"attachment": upload})

        assert result.is_valid
        assert "attachment" not in result.values

    async def test_field_renderer_outputs_labels_options_and_errors(self) -> None:
        form = ExampleForm()
        result = await form.parse({"preferred_name": "", "pronouns": "she|her"})
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

    async def test_form_renderer_outputs_form_actions_and_csrf_hidden_field(
        self,
    ) -> None:
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

    async def test_form_renderer_outputs_form_level_errors_and_upload_encoding(
        self,
    ) -> None:
        class UploadErrorForm(Form):
            attachment = FileUploadField()

            async def validate(self, field_name: str | None = None) -> bool:
                inherited = await super().validate(field_name)
                local = True
                if field_name is None:
                    self.add_error(None, "Form-level upload problem.")
                    local = False
                return inherited and local

        form = UploadErrorForm()
        await form.parse({"attachment": UploadedFile(filename="document.pdf")})
        renderer = TemplateFormRenderer(_forms_templates())

        html = renderer.render_form(form)

        assert 'enctype="multipart/form-data"' in html
        assert "Form-level upload problem." in html
        assert 'type="file"' in html

    async def test_form_renderer_uses_upload_encoding_for_custom_file_widget(
        self,
    ) -> None:
        class CustomUploadWidgetForm(Form):
            attachment = FileUploadField(widget="custom-file")

        renderer = TemplateFormRenderer(
            _forms_templates(),
            widgets={"custom-file": "forms/widgets/file.html"},
        )

        html = renderer.render_form(CustomUploadWidgetForm())

        assert 'enctype="multipart/form-data"' in html
        assert 'type="file"' in html

    async def test_form_renderer_raises_clear_error_for_unknown_widget(self) -> None:
        class UnknownWidgetForm(Form):
            value = TextField(widget="missing-widget")

        renderer = TemplateFormRenderer(_forms_templates())

        with pytest.raises(UnknownWidgetError, match="missing-widget.*value"):
            renderer.render_field(UnknownWidgetForm(), "value")

    async def test_form_renderer_accepts_custom_widget_mapping(self) -> None:
        class CustomWidgetForm(Form):
            value = TextField(widget="custom-text")

        renderer = TemplateFormRenderer(
            _forms_templates(),
            widgets={"custom-text": "forms/widgets/text.html"},
        )

        html = renderer.render_field(
            CustomWidgetForm(values={"value": "custom"}), "value"
        )

        assert 'name="value"' in html
        assert 'value="custom"' in html

    async def test_template_rendering_helpers_return_safe_html(self) -> None:
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

    async def test_phone_contact_renderer_outputs_mapped_fields_and_state(self) -> None:
        form = PhoneContactForm(
            options={
                "country": COUNTRY_OPTIONS,
                "region": SUBDIVISION_OPTIONS,
            },
            values={"country": "AU", "region": "VIC", "mobile": "0412345678"},
        )
        await form.parse({"country": "AU", "region": "VIC", "mobile": "<script>"})
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
        assert "Not verified" not in html

    async def test_phone_contact_renderer_outputs_status_without_phone_errors(
        self,
    ) -> None:
        form = PhoneContactForm(
            options={
                "country": COUNTRY_OPTIONS,
                "region": SUBDIVISION_OPTIONS,
            },
            values={"country": "AU", "region": "VIC", "mobile": "0412345678"},
        )
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

        assert ">Not verified<" in html

    async def test_phone_contact_fragment_preserves_disabled_fields_and_mapped_names(
        self,
    ) -> None:
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

    async def test_phone_contact_rendering_helpers_return_safe_html(self) -> None:
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

    async def test_phone_contact_renderer_rejects_unknown_field_mapping(self) -> None:
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

    async def test_phone_contact_fragment_rejects_unknown_field_mapping(self) -> None:
        renderer = TemplateFormRenderer(_forms_templates())
        form = PhoneContactForm()

        with pytest.raises(
            PhoneContactWidgetError, match="phone_field='missing_mobile'"
        ):
            renderer.render_phone_contact_fields(
                form,
                subdivision_field="region",
                phone_field="missing_mobile",
            )

    async def test_phone_contact_renderer_rejects_wrong_field_type(self) -> None:
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

    async def test_phone_contact_prefix_uses_template_driven_empty_class(self) -> None:
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

    async def test_phone_contact_control_sources_unfiltered_options(self) -> None:
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

    async def test_phone_contact_control_filters_options_and_rejects_filtered_country(
        self,
    ) -> None:
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
        await form.parse({"country": "NZ", "mobile": "+64211234567"})

        validation = control.validate(form)

        assert control.country_options() == {"AU": "Australia"}
        assert not validation.is_valid
        assert "country" in form.errors
        assert "Choose a valid country." in form.errors["country"]

    async def test_phone_contact_control_filters_and_rejects_filtered_subdivision(
        self,
    ) -> None:
        control = PhoneContactControl(
            country_field="country",
            subdivision_field="region",
            phone_field="mobile",
            subdivision_filter=lambda subdivision, _country: (
                subdivision.code == "AU-VIC"
            ),
        )
        form = PhoneContactForm(
            options={
                "country": control.country_options(),
                "region": control.subdivision_options("AU"),
            },
        )
        control.apply_state(form, "AU")
        await form.parse(
            {"country": "AU", "region": "AU-NSW", "mobile": "0412 345 678"}
        )

        validation = control.validate(form)

        assert control.subdivision_options("AU") == {"AU-VIC": "Victoria"}
        assert not validation.is_valid
        assert "region" in form.errors

    async def test_phone_contact_control_validates_and_normalises_phone_number(
        self,
    ) -> None:
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
        await form.parse(
            {"country": "AU", "region": "AU-VIC", "mobile": "0412 345 678"}
        )

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

    async def test_phone_contact_control_rejects_invalid_phone_number(self) -> None:
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
        await form.parse({"country": "AU", "region": "AU-VIC", "mobile": "not a phone"})

        validation = control.validate(form)

        assert not validation.is_valid
        assert form.errors["mobile"] == ["Phone contact number is invalid."]

    async def test_phone_contact_control_declares_default_htmx_field_handler(
        self,
    ) -> None:
        handler = PhoneContactForm.phone_contact.dependent_fields_handler()

        assert isinstance(handler, FieldHandler)
        assert handler.path == "/phone-contact/fields"
        assert handler.name == "phone-contact-fields"
        assert handler.methods == frozenset({"GET"})
        assert handler.htmx is True
        assert handler.include_in_schema is False

    async def test_phone_contact_control_rejects_empty_handler_tuple(self) -> None:
        with pytest.raises(
            PhoneContactError,
            match="requires at least one field handler",
        ):
            PhoneContactControl(
                country_field="country",
                subdivision_field="region",
                phone_field="mobile",
                handlers=(),
            )

    async def test_form_control_discovers_declared_phone_contact_control(self) -> None:
        assert (
            form_control(PhoneContactForm, "phone_contact")
            is PhoneContactForm.phone_contact
        )

    async def test_phone_contact_handler_declaration_does_not_register_routes(
        self,
    ) -> None:
        app = FastAPI()

        await PhoneContactForm().parse({"country": "AU"})

        assert [route.path for route in app.routes] == [
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        ]

    async def test_phone_contact_field_handler_registers_htmx_fragment_route(
        self,
    ) -> None:
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

        response = WybraTestClient(app).get(
            "/phone-contact/fields?country=AU",
            headers={"HX-Request": "true"},
        )

        assert getattr(route, "include_in_schema", True) is False
        assert "GET" in getattr(route, "methods", set())
        assert response.status_code == 200
        assert 'id="test-phone-fields"' in response.text
        assert "Victoria" in response.text
        assert "🇦🇺 +61" in response.text

    async def test_phone_contact_field_handler_mounts_relative_to_form_route(
        self,
    ) -> None:
        router = APIRouter()

        @router.get("/delivery", name="delivery:form")
        async def delivery_form() -> PlainTextResponse:
            return PlainTextResponse("delivery")

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
            form_route_name="delivery:form",
        )
        app = FastAPI()
        app.include_router(router)

        response = WybraTestClient(app).get(
            "/delivery/phone-contact/fields?country=AU",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Victoria" in response.text

    async def test_phone_contact_field_handler_rejects_non_htmx_request(self) -> None:
        router = APIRouter()
        register_phone_contact_field_handlers(
            router,
            control=PhoneContactForm.phone_contact,
            form_factory=lambda _request: PhoneContactForm(),
            templates=lambda _request: _forms_templates(),
        )
        app = FastAPI()
        app.include_router(router)

        response = WybraTestClient(app).get("/phone-contact/fields?country=AU")

        assert response.status_code == 404

    async def test_phone_contact_renderer_resolves_control_handler_url(self) -> None:
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

        response = WybraTestClient(app).get("/form")

        assert response.status_code == 200
        assert 'hx-get="http://testserver/phone-contact/fields"' in response.text
        assert 'hx-include="closest .wybra-phone-contact"' in response.text

    async def test_phone_contact_renderer_scopes_duplicate_control_handler_names(
        self,
    ) -> None:
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
        client = WybraTestClient(app)

        delivery_response = client.get("/delivery")
        billing_response = client.get("/billing")

        assert delivery_response.status_code == 200
        assert billing_response.status_code == 200
        assert (
            'hx-get="http://testserver/phone-contact/fields"' in delivery_response.text
        )
        assert (
            'hx-get="http://testserver/billing-phone-contact/fields"'
            in billing_response.text
        )

    async def test_forms_rendering_context_rejects_incomplete_csrf_context(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="csrf_token"):
            forms_rendering_context(
                _forms_templates(),
                csrf={"csrf_field_name": "csrf_token"},
            )

    async def test_forms_context_layers_valid_csrf_and_rendering_helpers(self) -> None:
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

        response = WybraTestClient(app).get("/form")

        assert response.status_code == 200
        assert response.json()["field_name"] == CSRF_FIELD_NAME
        assert 'name="csrf_token"' in response.json()["csrf_html"]

    async def test_forms_static_css_resource_is_available(self) -> None:
        resource = first_existing_resource(
            (PackageResourceSource(package="wybra.forms", directory="static"),),
            "styles/forms.css",
        )

        assert resource is not None

    async def test_forms_static_css_contains_phone_contact_widget_styles(self) -> None:
        resource = first_existing_resource(
            (PackageResourceSource(package="wybra.forms", directory="static"),),
            "styles/forms.css",
        )
        assert resource is not None
        css = resource.read_text(encoding="utf-8")

        assert ".wybra-phone-contact-control" in css
        assert ".wybra-phone-contact-prefix" in css
        assert ".wybra-phone-contact-status--unverified" in css

    async def test_forms_settings_generates_local_secret(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="wybra.forms.settings")

        settings = FormsSettings()

        assert settings.token_secret
        assert settings.cookie_secure is False
        assert "Generated startup-local CSRF token secret." in caplog.text

    async def test_forms_settings_load_settings_uses_config_service_sources(
        self,
    ) -> None:
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

        settings = FormsSettings.load_settings(
            config,
            deployment_environment="production",
        )

        assert settings.deployment_environment == "production"
        assert settings.token_secret == "production-csrf-secret"
        assert settings.cookie_secure is True

    async def test_forms_settings_exposes_csrf_credential_references(self) -> None:
        settings = FormsSettings(csrf_token_secret_source="keychain")

        references = settings.credential_references()

        assert [
            (
                reference.name,
                reference.key,
                reference.owner,
                reference.source,
                reference.required,
                reference.rotation_role,
            )
            for reference in references
        ] == [
            (
                "csrf",
                CSRF_TOKEN_SECRET_KEY_CURRENT,
                "forms",
                "keychain",
                True,
                "current",
            ),
            (
                "csrf-prev",
                CSRF_TOKEN_SECRET_KEY_PREVIOUS,
                "forms",
                "keychain",
                False,
                "previous",
            ),
        ]
        assert all(not hasattr(reference, "value") for reference in references)

    async def test_forms_settings_credential_references_ignore_inline_fallback_secret(
        self,
    ) -> None:
        settings = FormsSettings(csrf_token_secret="inline-csrf-secret")

        assert settings.credential_references() == ()

    async def test_forms_settings_load_settings_rejects_blank_token_secret(
        self,
    ) -> None:
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
    async def test_forms_setup_provides_forms_capability(self, tmp_path) -> None:
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
    async def test_forms_setup_uses_keychain_csrf_secret_before_fallback(self) -> None:
        site = _forms_site(
            {
                "wybra.forms": {
                    "csrf_token_secret_source": "keychain",
                    "csrf_cookie_secure": "true",
                },
            },
            deployment_environment="production",
            environ={"CSRF_SECRET_KEY": "fallback-csrf-secret"},
        )
        site.provide_capability(
            SecretsCapability,
            FakeSecretsCapability(
                {("keychain", CSRF_TOKEN_SECRET_KEY_CURRENT): "keychain-csrf-secret"}
            ),
        )

        await setup_forms_site(site)

        assert site.app.state.csrf.secret == "keychain-csrf-secret"
        assert site.app.state.csrf.cookie_secure is True

    @pytest.mark.anyio
    async def test_forms_setup_uses_keychain_previous_csrf_secrets(self) -> None:
        site = _forms_site(
            {
                "wybra.forms": {
                    "csrf_token_secret_source": "keychain",
                    "csrf_cookie_secure": "true",
                },
            },
            deployment_environment="production",
        )
        site.provide_capability(
            SecretsCapability,
            FakeSecretsCapability(
                {
                    ("keychain", CSRF_TOKEN_SECRET_KEY_CURRENT): "current-csrf-secret",
                    (
                        "keychain",
                        CSRF_TOKEN_SECRET_KEY_PREVIOUS,
                    ): "previous-csrf-secret,older-csrf-secret",
                }
            ),
        )

        await setup_forms_site(site)

        assert site.app.state.csrf.secret == "current-csrf-secret"
        assert site.app.state.csrf.previous_secrets == (
            "previous-csrf-secret",
            "older-csrf-secret",
        )

    @pytest.mark.anyio
    async def test_forms_setup_fails_when_previous_csrf_secret_resolution_errors(
        self,
    ) -> None:
        site = _forms_site(
            {
                "wybra.forms": {
                    "csrf_token_secret_source": "keychain",
                    "csrf_cookie_secure": "true",
                },
            },
            deployment_environment="production",
        )
        site.provide_capability(
            SecretsCapability,
            FailingPreviousCsrfSecretsCapability(
                {("keychain", CSRF_TOKEN_SECRET_KEY_CURRENT): "current-csrf-secret"}
            ),
        )

        with pytest.raises(ConfigurationError, match=CSRF_TOKEN_SECRET_KEY_PREVIOUS):
            await setup_forms_site(site)

    @pytest.mark.anyio
    async def test_forms_setup_falls_back_to_configured_csrf_secret(self) -> None:
        site = _forms_site(
            {
                "wybra.forms": {
                    "csrf_token_secret_source": "keychain",
                    "csrf_cookie_secure": "true",
                },
            },
            deployment_environment="production",
            environ={"CSRF_SECRET_KEY": "fallback-csrf-secret"},
        )

        await setup_forms_site(site)

        assert site.app.state.csrf.secret == "fallback-csrf-secret"

    @pytest.mark.anyio
    async def test_forms_setup_fails_when_keychain_and_fallback_are_missing(
        self,
    ) -> None:
        site = _forms_site(
            {
                "wybra.forms": {
                    "csrf_token_secret_source": "keychain",
                    "csrf_cookie_secure": "true",
                },
            },
            deployment_environment="production",
        )
        site.provide_capability(SecretsCapability, FakeSecretsCapability())

        with pytest.raises(ConfigurationError, match=CSRF_TOKEN_SECRET_KEY_CURRENT):
            await setup_forms_site(site)

    @pytest.mark.anyio
    async def test_forms_setup_does_not_use_crypto_keyring_as_csrf_secret(self) -> None:
        site = _forms_site(
            {
                "secrets.crypto": {
                    "source": "environment",
                    "current_key": "WYBRA_SECRET_KEY",
                },
                "wybra.forms": {"csrf_cookie_secure": "true"},
            },
            deployment_environment="production",
            environ={"WYBRA_SECRET_KEY": "crypto-secret"},
        )

        with pytest.raises(ConfigurationError, match="stable CSRF token secret"):
            await setup_forms_site(site)

    @pytest.mark.anyio
    async def test_forms_setup_finalises_csrf_cookie_when_requested(
        self, tmp_path
    ) -> None:
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

        with WybraTestClient(app) as client:
            response = client.get("/form")
            partial_response = client.get("/partials/form")

        assert response.status_code == 200
        assert CSRF_COOKIE_NAME in response.cookies
        assert partial_response.status_code == 200
        assert CSRF_COOKIE_NAME not in partial_response.cookies

    async def test_validate_forms_target_is_available(
        self, monkeypatch, tmp_path
    ) -> None:
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

    async def test_validate_forms_reports_loaded_settings(self) -> None:
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
            check.description.startswith("forms settings load")
            for check in result.checks
        )

    async def test_validate_forms_accepts_keychain_backed_csrf_config(self) -> None:
        from wybra.forms.validation import validate_forms

        result = validate_forms(
            type(
                "Settings",
                (),
                {
                    "modules": ("wybra.forms",),
                    "deployment_environment": "production",
                    "config": ConfigService(
                        [
                            MappingConfigSource(
                                {
                                    "app": {"modules": ("wybra.forms",)},
                                    "wybra.forms": {
                                        "csrf_token_secret_source": "keychain",
                                        "csrf_cookie_secure": "true",
                                    },
                                }
                            )
                        ],
                    ),
                },
            )()
        )

        assert result.is_ok
