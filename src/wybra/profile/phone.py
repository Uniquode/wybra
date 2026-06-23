from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
import pycountry
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

from wybra.profile.exceptions import ProfileInputError


@dataclass(frozen=True, slots=True)
class CountryChoice:
    code: str
    name: str
    dial_prefix: str
    flag: str


@dataclass(frozen=True, slots=True)
class SubdivisionChoice:
    code: str
    name: str
    country_code: str


@dataclass(frozen=True, slots=True)
class NormalisedPhoneContact:
    country_code: str
    normalised_number: str
    number_type: str
    sms_capable: bool
    subdivision_code: str | None = None


def normalise_phone_contact(
    number: str,
    *,
    country_code: str | None,
    subdivision_code: str | None = None,
) -> NormalisedPhoneContact:
    phone_number = _phone_number_value(number)
    normalised_country_code = _optional_country_code(country_code)
    normalised_subdivision_code = _optional_subdivision_code(
        subdivision_code,
        normalised_country_code,
    )
    if not phone_number.startswith("+") and normalised_country_code is None:
        raise ProfileInputError(
            "Phone contact country is required for local phone numbers."
        )
    try:
        parsed = phonenumbers.parse(phone_number, normalised_country_code)
    except NumberParseException as exc:
        raise ProfileInputError("Phone contact number is invalid.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ProfileInputError("Phone contact number is invalid.")

    resolved_country_code = (
        normalised_country_code or phonenumbers.region_code_for_number(parsed)
    )
    if resolved_country_code is None:
        raise ProfileInputError("Phone contact country could not be resolved.")
    number_type = phonenumbers.number_type(parsed)
    return NormalisedPhoneContact(
        country_code=resolved_country_code,
        subdivision_code=normalised_subdivision_code,
        normalised_number=phonenumbers.format_number(parsed, PhoneNumberFormat.E164),
        number_type=_phone_number_type_name(number_type),
        sms_capable=_is_sms_capable(number_type),
    )


def country_choices() -> tuple[CountryChoice, ...]:
    countries: list[CountryChoice] = []
    for country in pycountry.countries:
        code = getattr(country, "alpha_2", None)
        name = getattr(country, "name", None)
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        countries.append(
            CountryChoice(
                code=code,
                name=name,
                dial_prefix=_dial_prefix(code),
                flag=country_flag(code),
            )
        )
    return tuple(sorted(countries, key=lambda country: country.name.casefold()))


def subdivision_choices(country_code: str) -> tuple[SubdivisionChoice, ...]:
    normalised_country_code = _country_code_value(country_code)
    subdivisions = pycountry.subdivisions.get(country_code=normalised_country_code)
    return tuple(
        sorted(
            (
                SubdivisionChoice(
                    code=subdivision.code,
                    name=subdivision.name,
                    country_code=normalised_country_code,
                )
                for subdivision in subdivisions
            ),
            key=lambda subdivision: subdivision.name.casefold(),
        )
    )


def country_flag(country_code: str) -> str:
    code = _country_code_value(country_code)
    return "".join(chr(0x1F1E6 + ord(character) - ord("A")) for character in code)


def _phone_number_value(value: str) -> str:
    if not isinstance(value, str):
        raise ProfileInputError("Phone contact number must be text.")
    phone_number = value.strip()
    if not phone_number:
        raise ProfileInputError("Phone contact number must not be blank.")
    return phone_number


def _optional_country_code(value: str | None) -> str | None:
    if value is None:
        return None
    return _country_code_value(value)


def _country_code_value(value: str) -> str:
    if not isinstance(value, str):
        raise ProfileInputError("Country code must be text.")
    country_code = value.strip().upper()
    if len(country_code) != 2 or pycountry.countries.get(alpha_2=country_code) is None:
        raise ProfileInputError("Country code must be a valid ISO 3166-1 alpha-2 code.")
    return country_code


def _optional_subdivision_code(
    value: str | None,
    country_code: str | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileInputError("Subdivision code must be text.")
    subdivision_code = value.strip().upper()
    if not subdivision_code:
        return None
    subdivision = pycountry.subdivisions.get(code=subdivision_code)
    if subdivision is None:
        raise ProfileInputError("Subdivision code must be a valid ISO 3166-2 code.")
    if country_code is not None and subdivision.country_code != country_code:
        raise ProfileInputError("Subdivision code must belong to the selected country.")
    return subdivision_code


def _dial_prefix(country_code: str) -> str:
    phone_country_code = phonenumbers.country_code_for_region(country_code)
    return f"+{phone_country_code}" if phone_country_code else ""


def _phone_number_type_name(number_type: int) -> str:
    return {
        PhoneNumberType.FIXED_LINE: "fixed_line",
        PhoneNumberType.MOBILE: "mobile",
        PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
        PhoneNumberType.TOLL_FREE: "toll_free",
        PhoneNumberType.PREMIUM_RATE: "premium_rate",
        PhoneNumberType.SHARED_COST: "shared_cost",
        PhoneNumberType.VOIP: "voip",
        PhoneNumberType.PERSONAL_NUMBER: "personal_number",
        PhoneNumberType.PAGER: "pager",
        PhoneNumberType.UAN: "uan",
        PhoneNumberType.VOICEMAIL: "voicemail",
    }.get(number_type, "unknown")


def _is_sms_capable(number_type: int) -> bool:
    return number_type in {
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }


__all__ = (
    "CountryChoice",
    "NormalisedPhoneContact",
    "SubdivisionChoice",
    "country_choices",
    "country_flag",
    "normalise_phone_contact",
    "subdivision_choices",
)
