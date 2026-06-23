from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from wybra.config import BaseSettings, ConfigDef, ConfigField, ConfigGroup, to_bool

PROFILE_CONFIG_SECTION: Final = "wybra.profile"

PREFERRED_NAME_FIELD: Final = "preferred_name"
DISPLAY_NAME_FIELD: Final = "display_name"
PRONOUNS_FIELD: Final = "pronouns"
PROFILE_LINKS_FIELD: Final = "profile_links"
BIO_FIELD: Final = "bio"
PHONE_CONTACTS_FIELD: Final = "phone_contacts"
DEFAULT_PRONOUN_OPTIONS: Final = (
    ("she", "her"),
    ("he", "his"),
    ("they", "their"),
)

SUPPORTED_EDITABLE_PROFILE_FIELDS: Final = frozenset(
    {
        PREFERRED_NAME_FIELD,
        DISPLAY_NAME_FIELD,
        PRONOUNS_FIELD,
        PROFILE_LINKS_FIELD,
        BIO_FIELD,
        PHONE_CONTACTS_FIELD,
    }
)
DEFAULT_EDITABLE_PROFILE_FIELDS: Final = (
    PREFERRED_NAME_FIELD,
    DISPLAY_NAME_FIELD,
    PRONOUNS_FIELD,
    PROFILE_LINKS_FIELD,
    BIO_FIELD,
    PHONE_CONTACTS_FIELD,
)


@dataclass(frozen=True, slots=True)
class ProfileFieldMetadata:
    name: str
    label: str
    kind: str
    max_length: int | None = None


@dataclass(frozen=True, slots=True)
class ProfilePronounOption:
    direct: str
    possessive: str

    @property
    def value(self) -> str:
        return f"{self.direct}|{self.possessive}"

    @property
    def label(self) -> str:
        return f"{self.direct} / {self.possessive}"


PROFILE_FIELD_METADATA: Final = {
    PREFERRED_NAME_FIELD: ProfileFieldMetadata(
        name=PREFERRED_NAME_FIELD,
        label="Preferred name",
        kind="text",
        max_length=120,
    ),
    DISPLAY_NAME_FIELD: ProfileFieldMetadata(
        name=DISPLAY_NAME_FIELD,
        label="Display name",
        kind="text",
        max_length=200,
    ),
    PRONOUNS_FIELD: ProfileFieldMetadata(
        name=PRONOUNS_FIELD,
        label="Pronouns",
        kind="pronouns",
    ),
    PROFILE_LINKS_FIELD: ProfileFieldMetadata(
        name=PROFILE_LINKS_FIELD,
        label="Profile links",
        kind="links",
    ),
    BIO_FIELD: ProfileFieldMetadata(
        name=BIO_FIELD,
        label="Bio",
        kind="textarea",
        max_length=1024,
    ),
    PHONE_CONTACTS_FIELD: ProfileFieldMetadata(
        name=PHONE_CONTACTS_FIELD,
        label="Phone contacts",
        kind="phone_contacts",
    ),
}


def to_editable_profile_fields(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        fields = tuple(field.strip() for field in value.split(",") if field.strip())
        return _validate_editable_profile_fields(fields)
    if isinstance(value, list | tuple):
        return _validate_editable_profile_fields(tuple(value))
    raise ValueError(
        "editable_fields must be a list, tuple, or comma-separated string."
    )


def _validate_editable_profile_fields(fields: tuple[object, ...]) -> tuple[str, ...]:
    invalid_types = tuple(field for field in fields if not isinstance(field, str))
    if invalid_types:
        raise ValueError("editable profile field names must be strings.")
    field_names = tuple(field for field in fields if isinstance(field, str))
    unknown = tuple(sorted(set(field_names) - SUPPORTED_EDITABLE_PROFILE_FIELDS))
    if unknown:
        raise ValueError("unknown editable profile field(s): " + ", ".join(unknown))
    return field_names


def to_pronoun_options(value: object) -> tuple[ProfilePronounOption, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = tuple(option.strip() for option in value.split(",") if option.strip())
    elif isinstance(value, list | tuple):
        values = tuple(value)
    else:
        raise ValueError(
            "pronoun_options must be a list, tuple, or comma-separated string."
        )
    return tuple(_pronoun_option(option) for option in values)


def _pronoun_option(value: object) -> ProfilePronounOption:
    if isinstance(value, ProfilePronounOption):
        return value
    if isinstance(value, str):
        direct, separator, possessive = value.partition("|")
        if separator and direct.strip() and possessive.strip():
            return ProfilePronounOption(
                direct=direct.strip(),
                possessive=possessive.strip(),
            )
    if isinstance(value, list | tuple) and len(value) == 2:
        direct, possessive = value
        if isinstance(direct, str) and isinstance(possessive, str):
            direct = direct.strip()
            possessive = possessive.strip()
            if direct and possessive:
                return ProfilePronounOption(direct=direct, possessive=possessive)
    raise ValueError("pronoun options must contain direct and possessive text.")


module_config: Final = ConfigDef(
    {
        PROFILE_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="editing_enabled",
                    default=True,
                    transform=to_bool,
                ),
                ConfigField(
                    name="editable_fields",
                    default=DEFAULT_EDITABLE_PROFILE_FIELDS,
                    transform=to_editable_profile_fields,
                ),
                ConfigField(
                    name="pronoun_options",
                    default=DEFAULT_PRONOUN_OPTIONS,
                    transform=to_pronoun_options,
                ),
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ProfileSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = PROFILE_CONFIG_SECTION

    editing_enabled: bool = True
    editable_fields: tuple[str, ...] = DEFAULT_EDITABLE_PROFILE_FIELDS
    pronoun_options: tuple[ProfilePronounOption, ...] = tuple(
        ProfilePronounOption(*option) for option in DEFAULT_PRONOUN_OPTIONS
    )


__all__ = (
    "BIO_FIELD",
    "DEFAULT_EDITABLE_PROFILE_FIELDS",
    "DEFAULT_PRONOUN_OPTIONS",
    "DISPLAY_NAME_FIELD",
    "PHONE_CONTACTS_FIELD",
    "PREFERRED_NAME_FIELD",
    "PROFILE_CONFIG_SECTION",
    "PROFILE_FIELD_METADATA",
    "PROFILE_LINKS_FIELD",
    "PRONOUNS_FIELD",
    "SUPPORTED_EDITABLE_PROFILE_FIELDS",
    "ProfileFieldMetadata",
    "ProfilePronounOption",
    "ProfileSettings",
    "module_config",
    "to_editable_profile_fields",
    "to_pronoun_options",
)
