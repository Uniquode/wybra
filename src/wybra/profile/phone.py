from __future__ import annotations

from wybra.forms.phone_contact import (
    CountryChoice,
    NormalisedPhoneContact,
    PhoneContactError,
    SubdivisionChoice,
    country_choices,
    country_flag,
    subdivision_choices,
)
from wybra.forms.phone_contact import (
    normalise_phone_contact as _normalise_phone_contact,
)
from wybra.profile.exceptions import ProfileInputError


def normalise_phone_contact(
    number: str,
    *,
    country_code: str | None,
    subdivision_code: str | None = None,
) -> NormalisedPhoneContact:
    try:
        return _normalise_phone_contact(
            number,
            country_code=country_code,
            subdivision_code=subdivision_code,
        )
    except PhoneContactError as exc:
        raise ProfileInputError(str(exc)) from exc


__all__ = (
    "CountryChoice",
    "NormalisedPhoneContact",
    "SubdivisionChoice",
    "country_choices",
    "country_flag",
    "normalise_phone_contact",
    "subdivision_choices",
)
