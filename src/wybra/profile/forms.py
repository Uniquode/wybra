from __future__ import annotations

from collections.abc import Mapping

from wybra.forms import (
    Form,
    FormResult,
    HiddenField,
    NormalisedPhoneContact,
    PhoneContactControl,
    PhoneContactValidation,
    SelectField,
    TextAreaField,
    TextField,
)
from wybra.profile.editing import PROFILE_BIO_MAX_LENGTH
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.models import UserProfile
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

PROFILE_FORM_FIELD_MAP = {
    PREFERRED_NAME_FIELD: (PREFERRED_NAME_FIELD,),
    DISPLAY_NAME_FIELD: (DISPLAY_NAME_FIELD,),
    PRONOUNS_FIELD: ("pronoun_pair",),
    PROFILE_LINKS_FIELD: ("profile_link_website",),
    BIO_FIELD: (BIO_FIELD,),
    PHONE_CONTACTS_FIELD: (
        "phone_country_code",
        "phone_subdivision_code",
        "phone_number",
    ),
}


class ProfileEditForm(Form):
    return_to = HiddenField(required=False)
    preferred_name = TextField(
        label="Preferred name",
        required=False,
        max_length=120,
    )
    display_name = TextField(
        label="Display name",
        required=False,
        max_length=200,
    )
    pronoun_pair = SelectField(
        label="Pronouns",
        required=False,
    )
    profile_link_website = TextField(
        label="Website",
        required=False,
    )
    bio = TextAreaField(
        label="Bio",
        required=False,
        max_length=PROFILE_BIO_MAX_LENGTH,
    )
    phone_country_code = SelectField(
        label="Country",
        required=False,
    )
    phone_subdivision_code = SelectField(
        label="State or region",
        required=False,
    )
    phone_number = TextField(
        label="Phone number",
        required=False,
    )
    phone_contact = PhoneContactControl(
        country_field="phone_country_code",
        subdivision_field="phone_subdivision_code",
        phone_field="phone_number",
    )

    def __init__(
        self,
        *,
        settings: ProfileSettings,
        values: dict[str, object] | None = None,
    ) -> None:
        self.settings = settings
        self._profile_field_data: dict[str, object] = {}
        self._phone_contact_validation: PhoneContactValidation | None = None
        form_values = values or {}
        phone_country_code = normalise_form_text(form_values.get("phone_country_code"))
        super().__init__(
            values=form_values,
            options={
                "pronoun_pair": {
                    option.value: option.label for option in settings.pronoun_options
                },
                "phone_country_code": self.phone_contact.country_options(),
                "phone_subdivision_code": (
                    self.phone_contact.subdivision_options(phone_country_code)
                ),
            },
        )
        self._apply_editability()
        self.phone_contact.apply_state(self, phone_country_code)

    def validate(self, field_name: str | None = None) -> bool:
        base_result = super().validate(field_name)
        local_result = True
        match field_name:
            case "preferred_name":
                local_result = self._validate_text_profile_field(
                    form_field="preferred_name",
                    profile_field=PREFERRED_NAME_FIELD,
                )
            case "display_name":
                local_result = self._validate_text_profile_field(
                    form_field="display_name",
                    profile_field=DISPLAY_NAME_FIELD,
                )
            case "bio":
                local_result = self._validate_text_profile_field(
                    form_field="bio",
                    profile_field=BIO_FIELD,
                )
            case "pronoun_pair":
                local_result = self._validate_pronouns()
            case "profile_link_website":
                local_result = self._validate_profile_link()
            case "phone_country_code" | "phone_subdivision_code":
                local_result = self._validate_phone_contact()
            case "phone_number":
                local_result = self._validate_phone_contact()
        return base_result and local_result

    def parse(self, data: Mapping[str, object]) -> FormResult:
        self._profile_field_data = {}
        self._phone_contact_validation = None
        self._apply_phone_country_options(
            normalise_form_text(data.get("phone_country_code"))
        )
        return super().parse(data)

    def profile_field_data(self) -> dict[str, object]:
        return dict(self._profile_field_data)

    def has_phone_contact_data(self) -> bool:
        value = self.values.get("phone_number")
        return isinstance(value, str) and bool(value)

    def phone_contact_data(self) -> dict[str, str | None]:
        return {
            "number": normalise_form_text(self.values.get("phone_number")) or "",
            "country_code": normalise_form_text(self.values.get("phone_country_code")),
            "subdivision_code": normalise_form_text(
                self.values.get("phone_subdivision_code")
            ),
        }

    def normalised_phone_contact(self) -> NormalisedPhoneContact | None:
        """Return the normalised phone contact after parse/validation succeeds."""
        validation = self._phone_contact_validation
        return validation.normalised if validation is not None else None

    def _apply_editability(self) -> None:
        editable_fields = set(self.settings.editable_fields)
        for profile_field, form_fields in PROFILE_FORM_FIELD_MAP.items():
            if profile_field not in editable_fields:
                for form_field in form_fields:
                    self.fields.pop(form_field, None)
                    self.values.pop(form_field, None)
                    self.raw_values.pop(form_field, None)
                    self.field_results.pop(form_field, None)
        self._result = FormResult(fields=self.field_results)

    def _apply_phone_country_options(self, country_code: str | None) -> None:
        field = self.fields.get("phone_subdivision_code")
        if isinstance(field, SelectField):
            field.choices = dict(self.phone_contact.subdivision_options(country_code))
        self.phone_contact.apply_state(self, country_code)

    def _validate_text_profile_field(
        self,
        *,
        form_field: str,
        profile_field: str,
    ) -> bool:
        result = self.field_results.get(form_field)
        if result is None:
            return True
        value = result.value
        if not isinstance(value, str):
            if value is None:
                self._profile_field_data[profile_field] = None
                return True
            self.add_error(form_field, "Profile text fields must be text.")
            return False
        text = value.strip()
        self._profile_field_data[profile_field] = text or None
        return True

    def _validate_pronouns(self) -> bool:
        allowed_pronouns = {option.value for option in self.settings.pronoun_options}
        pronouns = self._pronouns_data()
        result = self.field_results.get("pronoun_pair")
        if result is None:
            self._profile_field_data[PRONOUNS_FIELD] = None
            return True
        raw_value = result.value
        if raw_value is None:
            self._profile_field_data[PRONOUNS_FIELD] = None
            return True
        if not isinstance(raw_value, str):
            self.add_error("pronoun_pair", "Pronouns must be a valid choice.")
            return False
        if raw_value and raw_value not in allowed_pronouns:
            self.add_error("pronoun_pair", "Choose a valid pronoun option.")
            return False
        if not raw_value:
            self._profile_field_data[PRONOUNS_FIELD] = None
            return True
        self._profile_field_data[PRONOUNS_FIELD] = pronouns
        return True

    def _validate_profile_link(self) -> bool:
        result = self.field_results.get("profile_link_website")
        if result is None:
            return True
        profile_link = result.value
        if not isinstance(profile_link, str):
            if profile_link is None:
                self._profile_field_data[PROFILE_LINKS_FIELD] = None
                return True
            self.add_error("profile_link_website", "Profile link URLs must be text.")
            return False
        url = profile_link.strip()
        if not url:
            self._profile_field_data[PROFILE_LINKS_FIELD] = None
            return True
        try:
            validate_safe_url(url)
        except ProfileInputError as exc:
            self.add_error("profile_link_website", str(exc))
            return False
        self._profile_field_data[PROFILE_LINKS_FIELD] = {"website": url}
        return True

    def _validate_phone_contact(self) -> bool:
        self._phone_contact_validation = self.phone_contact.validate(self)
        return self._phone_contact_validation.is_valid

    def _pronouns_data(self) -> dict[str, str] | None:
        result = self.field_results.get("pronoun_pair")
        if result is None:
            return None
        pronoun_pair = result.value
        if not isinstance(pronoun_pair, str) or not pronoun_pair:
            return None
        direct, separator, possessive = pronoun_pair.partition("|")
        if not separator:
            return {"direct": direct, "possessive": ""}
        return {"direct": direct, "possessive": possessive}


def profile_form_values(profile: UserProfile | None) -> dict[str, object]:
    if profile is None:
        return {
            "preferred_name": "",
            "display_name": "",
            "bio": "",
            "pronoun_pair": "",
            "profile_link_website": "",
        }
    pronouns = profile.pronouns if isinstance(profile.pronouns, dict) else {}
    links = profile.website_links if isinstance(profile.website_links, dict) else {}
    return {
        "preferred_name": profile.preferred_name or "",
        "display_name": profile.display_name or "",
        "bio": profile.bio or "",
        "pronoun_pair": _profile_pronoun_pair(pronouns),
        "profile_link_website": links.get("website", ""),
    }


def _profile_pronoun_pair(pronouns: Mapping[str, object]) -> str:
    direct = pronouns.get("direct")
    possessive = pronouns.get("possessive")
    if isinstance(direct, str) and isinstance(possessive, str):
        return f"{direct}|{possessive}"
    return ""


__all__ = ("ProfileEditForm", "profile_form_values")
