from __future__ import annotations

from collections.abc import Mapping
from html import escape

from wybra.profile.exceptions import ProfileInputError
from wybra.profile.settings import (
    BIO_FIELD,
    DISPLAY_NAME_FIELD,
    PREFERRED_NAME_FIELD,
    PROFILE_LINKS_FIELD,
    PRONOUNS_FIELD,
    ProfileSettings,
)
from wybra.profile.types import ProfileFieldValue, ProfileLinks, Pronouns
from wybra.profile.utils import validate_safe_url

PROFILE_BIO_MAX_LENGTH = 1024


def profile_field_values(
    data: Mapping[str, object],
    *,
    settings: ProfileSettings,
) -> dict[str, ProfileFieldValue]:
    _reject_disabled_fields(data, settings)
    values: dict[str, ProfileFieldValue] = {}
    for field_name, value in data.items():
        match field_name:
            case "preferred_name":
                values[PREFERRED_NAME_FIELD] = _optional_text(value)
            case "display_name":
                values[DISPLAY_NAME_FIELD] = _optional_text(value)
            case "pronouns":
                values[PRONOUNS_FIELD] = _pronouns_value(value)
            case "profile_links":
                values[PROFILE_LINKS_FIELD] = _profile_links_value(value)
            case "bio":
                values[BIO_FIELD] = _bio_value(value)
            case "phone_contacts":
                raise ProfileInputError(
                    "Phone contacts must be saved through phone contact handling."
                )
            case _:
                raise ProfileInputError(
                    f"Unknown profile field submitted: {field_name}."
                )
    return values


def render_profile_bio(value: str | None) -> str:
    return escape(value or "", quote=False)


def _reject_disabled_fields(
    data: Mapping[str, object],
    settings: ProfileSettings,
) -> None:
    editable_fields = set(settings.editable_fields)
    disabled = tuple(field for field in data if field not in editable_fields)
    if disabled:
        raise ProfileInputError(
            "Profile field is not editable: " + ", ".join(sorted(disabled))
        )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileInputError("Profile text fields must be text.")
    text = value.strip()
    return text or None


def _bio_value(value: object) -> str | None:
    bio = _optional_text(value)
    if bio is not None and len(bio) > PROFILE_BIO_MAX_LENGTH:
        raise ProfileInputError(
            f"Bio must be {PROFILE_BIO_MAX_LENGTH} characters or fewer."
        )
    return bio


def _pronouns_value(value: object) -> Pronouns | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProfileInputError("Pronouns must be submitted as structured values.")
    pronouns: Pronouns = {}
    for key in ("direct", "possessive"):
        raw_value = value.get(key)
        if raw_value is None:
            continue
        if not isinstance(raw_value, str):
            raise ProfileInputError("Pronoun values must be text.")
        pronoun = raw_value.strip()
        if pronoun:
            pronouns[key] = pronoun
    return pronouns or None


def _profile_links_value(value: object) -> ProfileLinks | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProfileInputError("Profile links must be submitted as structured values.")
    links: ProfileLinks = {}
    for key, raw_url in value.items():
        if not isinstance(key, str) or not isinstance(raw_url, str):
            raise ProfileInputError("Profile link names and URLs must be text.")
        link_name = key.strip()
        url = raw_url.strip()
        if not link_name or not url:
            continue
        validate_safe_url(url)
        if link_name != "website":
            raise ProfileInputError("Profile link name is not supported.")
        links["website"] = url
    return links or None


__all__ = (
    "PROFILE_BIO_MAX_LENGTH",
    "profile_field_values",
    "render_profile_bio",
)
