from __future__ import annotations

import uuid
from collections.abc import Mapping

from tortoise.models import Model

from wybra.db import DbConnection
from wybra.forms import (
    Form,
    FormResult,
    HiddenField,
    ModelForm,
    PhoneContactControl,
    PhoneContactValidation,
    SaveResult,
    SelectField,
    TextAreaField,
    TextField,
)
from wybra.profile.editing import PROFILE_BIO_MAX_LENGTH
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.models import UserProfile
from wybra.profile.persistence import save_phone_contact_in_transaction
from wybra.profile.settings import (
    BIO_FIELD,
    DISPLAY_NAME_FIELD,
    PHONE_CONTACTS_FIELD,
    PREFERRED_NAME_FIELD,
    PROFILE_LINKS_FIELD,
    PRONOUNS_FIELD,
    ProfileSettings,
)
from wybra.profile.utils import normalise_form_text, validate_safe_url


class _OptionalTextField(TextField):
    def to_model_value(self, value: object) -> object:
        return normalise_form_text(value)


class _OptionalTextAreaField(TextAreaField):
    def to_model_value(self, value: object) -> object:
        return normalise_form_text(value)


class _PronounsField(SelectField):
    def from_model_value(self, value: object) -> object:
        if not isinstance(value, Mapping):
            return None
        direct = value.get("direct")
        possessive = value.get("possessive")
        if not isinstance(direct, str):
            return None
        return f"{direct}|{possessive}" if isinstance(possessive, str) else direct

    def to_model_value(self, value: object) -> object:
        if not isinstance(value, str) or not value:
            return None
        direct, separator, possessive = value.partition("|")
        return {
            "direct": direct,
            "possessive": possessive if separator else "",
        }


class _WebsiteLinksField(TextField):
    def from_model_value(self, value: object) -> object:
        return value.get("website") if isinstance(value, Mapping) else None

    def to_model_value(self, value: object) -> object:
        website = normalise_form_text(value)
        return {"website": website} if website else None


class ProfileDetailsForm(ModelForm):
    preferred_name = _OptionalTextField(
        label="Preferred name", required=False, max_length=120
    )
    display_name = _OptionalTextField(
        label="Display name", required=False, max_length=200
    )
    pronouns = _PronounsField(label="Pronouns", required=False)
    website_links = _WebsiteLinksField(label="Website", required=False)
    bio = _OptionalTextAreaField(
        label="Bio", required=False, max_length=PROFILE_BIO_MAX_LENGTH
    )

    class Meta:
        model = UserProfile
        fields = (
            "preferred_name",
            "display_name",
            "pronouns",
            "website_links",
            "bio",
            "version",
        )

    def __init__(
        self,
        *,
        settings: ProfileSettings,
        user_id: uuid.UUID,
        instance: UserProfile | None,
        connection: DbConnection | None,
    ) -> None:
        self.settings = settings
        self.user_id = user_id
        super().__init__(
            instance=instance,
            connection=connection,
            defaults={"version": 0} if instance is None else None,
            options={
                "pronouns": {
                    option.value: option.label for option in settings.pronoun_options
                }
            },
        )
        self._apply_editability()

    async def validate(self, field_name: str | None = None) -> bool:
        inherited = await super().validate(field_name)
        if field_name != "website_links":
            return inherited
        value = self.values.get("website_links")
        if not isinstance(value, str) or not value.strip():
            return inherited
        try:
            validate_safe_url(value.strip())
        except ProfileInputError as exc:
            self.add_error("website_links", str(exc))
            return False
        return inherited

    def create_instance(self, model: type[Model]) -> Model:
        del model
        return UserProfile(user_id=self.user_id)

    def has_submitted_profile_values(self) -> bool:
        return any(
            self.result.fields[name].value is not None
            for name in self.fields
            if name != "version"
        )

    async def save_with_phone_contact(
        self,
        phone_contact: Mapping[str, str | None] | None,
    ) -> SaveResult:
        """Persist profile details and an optional phone contact atomically.

        The database client remains an implementation detail of the form and
        persistence layers; callers only supply already-validated phone data.
        """
        async with self._writer_transaction() as client:
            result = await self._save_with_client(client)
            if result.affected_count == 0 and self.errors:
                return result
            if phone_contact is not None:
                await save_phone_contact_in_transaction(
                    client,
                    self.user_id,
                    number=phone_contact["number"] or "",
                    country_code=phone_contact["country_code"],
                    subdivision_code=phone_contact["subdivision_code"],
                )
            return result

    def _apply_editability(self) -> None:
        editable = set(self.settings.editable_fields)
        profile_fields = {
            "preferred_name": PREFERRED_NAME_FIELD,
            "display_name": DISPLAY_NAME_FIELD,
            "pronouns": PRONOUNS_FIELD,
            "website_links": PROFILE_LINKS_FIELD,
            "bio": BIO_FIELD,
        }
        for form_field, profile_field in profile_fields.items():
            if profile_field not in editable:
                self.fields.pop(form_field, None)
                self.values.pop(form_field, None)
                self.raw_values.pop(form_field, None)
                self.field_results.pop(form_field, None)
        self._result = FormResult(fields=dict(self.field_results))


class ProfileEditForm(Form):
    """The intentionally bespoke phone-contact portion of profile editing."""

    return_to = HiddenField(required=False)
    phone_country_code = SelectField(label="Country", required=False)
    phone_subdivision_code = SelectField(label="State or region", required=False)
    phone_number = TextField(label="Phone number", required=False)
    phone_contact = PhoneContactControl(
        country_field="phone_country_code",
        subdivision_field="phone_subdivision_code",
        phone_field="phone_number",
    )

    def __init__(
        self,
        *,
        settings: ProfileSettings,
        values: Mapping[str, object] | None = None,
    ) -> None:
        self.settings = settings
        self._phone_contact_validation: PhoneContactValidation | None = None
        form_values = values or {}
        country = normalise_form_text(form_values.get("phone_country_code"))
        super().__init__(
            values=form_values,
            options={
                "phone_country_code": self.phone_contact.country_options(),
                "phone_subdivision_code": self.phone_contact.subdivision_options(
                    country
                ),
            },
        )
        if PHONE_CONTACTS_FIELD not in settings.editable_fields:
            for name in (
                "phone_country_code",
                "phone_subdivision_code",
                "phone_number",
            ):
                self.fields.pop(name, None)
        self.phone_contact.apply_state(self, country)

    async def validate(self, field_name: str | None = None) -> bool:
        inherited = await super().validate(field_name)
        if field_name in {
            "phone_country_code",
            "phone_subdivision_code",
            "phone_number",
        }:
            self._phone_contact_validation = self.phone_contact.validate(self)
            return inherited and self._phone_contact_validation.is_valid
        return inherited

    async def parse(self, data: Mapping[str, object]):
        self._phone_contact_validation = None
        country = normalise_form_text(data.get("phone_country_code"))
        field = self.fields.get("phone_subdivision_code")
        if isinstance(field, SelectField):
            field.choices = dict(self.phone_contact.subdivision_options(country))
        self.phone_contact.apply_state(self, country)
        return await super().parse(data)

    def has_phone_contact_data(self) -> bool:
        return bool(normalise_form_text(self.values.get("phone_number")))

    def phone_contact_data(self) -> dict[str, str | None]:
        return {
            "number": normalise_form_text(self.values.get("phone_number")) or "",
            "country_code": normalise_form_text(self.values.get("phone_country_code")),
            "subdivision_code": normalise_form_text(
                self.values.get("phone_subdivision_code")
            ),
        }

    def normalised_phone_contact(self):
        validation = self._phone_contact_validation
        return validation.normalised if validation is not None else None


__all__ = ("ProfileDetailsForm", "ProfileEditForm")
