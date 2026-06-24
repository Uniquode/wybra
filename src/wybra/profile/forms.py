from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from wybra.forms import (
    Form,
    FormResult,
    HiddenField,
    SelectField,
    TextAreaField,
    TextField,
)
from wybra.profile.editing import PROFILE_BIO_MAX_LENGTH
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.models import UserProfile
from wybra.profile.phone import (
    country_choices,
    normalise_phone_contact,
    subdivision_choices,
)
from wybra.profile.settings import (
    BIO_FIELD,
    DISPLAY_NAME_FIELD,
    PHONE_CONTACTS_FIELD,
    PREFERRED_NAME_FIELD,
    PROFILE_LINKS_FIELD,
    PRONOUNS_FIELD,
    ProfileSettings,
)

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

    def __init__(
        self,
        *,
        settings: ProfileSettings,
        values: dict[str, object] | None = None,
    ) -> None:
        self.settings = settings
        self._profile_field_data: dict[str, object] = {}
        form_values = values or {}
        phone_country_code = _optional_form_text(form_values.get("phone_country_code"))
        super().__init__(
            values=form_values,
            options={
                "pronoun_pair": {
                    option.value: option.label for option in settings.pronoun_options
                },
                "phone_country_code": {
                    country.code: country.name for country in country_choices()
                },
                "phone_subdivision_code": _subdivision_options(phone_country_code),
            },
        )
        self._apply_editability()
        self._apply_phone_country_state(phone_country_code)

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
            case "phone_number":
                local_result = self._validate_phone_contact()
        return base_result and local_result

    def parse(self, data: Mapping[str, object]) -> FormResult:
        self._profile_field_data = {}
        self._apply_phone_country_options(
            _optional_form_text(data.get("phone_country_code"))
        )
        return super().parse(data)

    def profile_field_data(self) -> dict[str, object]:
        return dict(self._profile_field_data)

    def has_phone_contact_data(self) -> bool:
        value = self.values.get("phone_number")
        return isinstance(value, str) and bool(value)

    def phone_contact_data(self) -> dict[str, str | None]:
        return {
            "number": _optional_form_text(self.values.get("phone_number")) or "",
            "country_code": _optional_form_text(self.values.get("phone_country_code")),
            "subdivision_code": _optional_form_text(
                self.values.get("phone_subdivision_code")
            ),
        }

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

    def _apply_phone_country_state(self, country_code: str | None) -> None:
        country_selected = _is_country_choice(country_code)
        for form_field in ("phone_subdivision_code", "phone_number"):
            if form_field in self.fields:
                self.fields[form_field].disabled = not country_selected

    def _apply_phone_country_options(self, country_code: str | None) -> None:
        field = self.fields.get("phone_subdivision_code")
        if isinstance(field, SelectField):
            field.choices = dict(_subdivision_options(country_code))
        self._apply_phone_country_state(country_code)

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
        pronouns = self._pronouns_data()
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
            _validate_safe_url(url)
        except ProfileInputError as exc:
            self.add_error("profile_link_website", str(exc))
            return False
        self._profile_field_data[PROFILE_LINKS_FIELD] = {"website": url}
        return True

    def _validate_phone_contact(self) -> bool:
        if not self.has_phone_contact_data():
            return True
        phone_contact = self.phone_contact_data()
        try:
            normalise_phone_contact(
                phone_contact["number"] or "",
                country_code=phone_contact["country_code"],
                subdivision_code=phone_contact["subdivision_code"],
            )
        except ProfileInputError as exc:
            self.add_error(_phone_error_field(str(exc)), str(exc))
            return False
        return True

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


def _optional_form_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _is_country_choice(country_code: str | None) -> bool:
    if country_code is None:
        return False
    normalised_country_code = country_code.strip().upper()
    return any(country.code == normalised_country_code for country in country_choices())


def _subdivision_options(country_code: str | None) -> Mapping[str, str]:
    if not _is_country_choice(country_code):
        return {}
    return {
        subdivision.code: subdivision.name
        for subdivision in subdivision_choices(country_code or "")
    }


def _phone_error_field(message: str) -> str:
    lower_message = message.casefold()
    if "country" in lower_message:
        return "phone_country_code"
    if "subdivision" in lower_message:
        return "phone_subdivision_code"
    return "phone_number"


def _validate_safe_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileInputError("Profile link URL scheme must be http or https.")
    if not parsed.netloc:
        raise ProfileInputError("Profile link URL must include a host.")
    if _contains_control_character(url):
        raise ProfileInputError("Profile link URL must not contain control characters.")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = ("ProfileEditForm", "profile_form_values")
