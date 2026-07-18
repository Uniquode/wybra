from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import phonenumbers
import pycountry
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRouter
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

from wybra.forms.fields import Form
from wybra.template.capabilities import TemplateCapability

type CountryFilter = Callable[[CountryChoice], bool]
type SubdivisionFilter = Callable[[SubdivisionChoice, CountryChoice], bool]
type FormFactory = Callable[[Request], Form]
type TemplateFactory = Callable[[Request], TemplateCapability]


class UrlForContext(Protocol):
    def url_for(self, name: str, /, **_path_params: Any) -> Any:
        """Resolve an application route URL by name."""
        ...


class PhoneContactError(ValueError):
    """Base for phone-contact control errors."""


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


@dataclass(frozen=True, slots=True)
class PhoneContactValidation:
    country_code: str | None
    subdivision_code: str | None
    number: str | None
    normalised: NormalisedPhoneContact | None
    is_valid: bool


@dataclass(frozen=True, slots=True)
class FieldHandler:
    path: str
    name: str
    methods: frozenset[str]
    htmx: bool = True
    include_in_schema: bool = False


def field_handler(
    path: str,
    *,
    name: str,
    methods: set[str] | frozenset[str] = frozenset(("GET",)),
) -> FieldHandler:
    return FieldHandler(
        path=path,
        name=name,
        methods=frozenset(method.upper() for method in methods),
    )


class PhoneContactControl:
    def __init__(
        self,
        *,
        country_field: str,
        subdivision_field: str,
        phone_field: str,
        handlers: tuple[FieldHandler, ...] | None = None,
        country_filter: CountryFilter | None = None,
        subdivision_filter: SubdivisionFilter | None = None,
    ) -> None:
        self.country_field = country_field
        self.subdivision_field = subdivision_field
        self.phone_field = phone_field
        if handlers is None:
            handlers = (
                field_handler(
                    "/phone-contact/fields",
                    name="phone-contact-fields",
                    methods={"GET"},
                ),
            )
        if not handlers:
            raise PhoneContactError(
                "Phone contact control requires at least one field handler."
            )
        self.handlers = handlers
        self.country_filter = country_filter
        self.subdivision_filter = subdivision_filter
        self._registered_handler_names: dict[str, str] = {}

    def country_options(self) -> Mapping[str, str]:
        return {country.code: country.name for country in self.countries()}

    def subdivision_options(self, country_code: str | None) -> Mapping[str, str]:
        return {
            subdivision.code: subdivision.name
            for subdivision in self.subdivisions(country_code)
        }

    def countries(self) -> tuple[CountryChoice, ...]:
        return country_choices(country_filter=self.country_filter)

    def subdivisions(self, country_code: str | None) -> tuple[SubdivisionChoice, ...]:
        country = self.country(country_code)
        if country is None:
            return ()
        return subdivision_choices(
            country.code,
            country_filter=self.country_filter,
            subdivision_filter=self.subdivision_filter,
        )

    def country(self, country_code: str | None) -> CountryChoice | None:
        normalised_country_code = optional_country_code(country_code)
        if normalised_country_code is None:
            return None
        for country in self.countries():
            if country.code == normalised_country_code:
                return country
        return None

    def phone_prefix(self, country_code: str | None) -> str:
        country = self.country(country_code)
        if country is None:
            return ""
        return f"{country.flag} {country.dial_prefix}".strip()

    def dependent_fields_handler(self) -> FieldHandler:
        return self.handlers[0]

    def dependent_fields_url(
        self,
        url_context: UrlForContext,
        *,
        route_name: str | None = None,
    ) -> str:
        handler = self.dependent_fields_handler()
        registered_name = self._registered_handler_names.get(handler.name, handler.name)
        return str(url_context.url_for(route_name or registered_name))

    def apply_state(self, form: Form, country_code: str | None) -> None:
        valid_country = self.country(country_code) is not None
        for field_name in (self.subdivision_field, self.phone_field):
            field = form.fields.get(field_name)
            if field is not None:
                field.disabled = not valid_country

    def validate(self, form: Form) -> PhoneContactValidation:
        from wybra.forms.phone_contact_rendering import form_text_value

        country_value = form_text_value(form, self.country_field)
        subdivision_value = form_text_value(form, self.subdivision_field)
        number_value = form_text_value(form, self.phone_field)
        normalised_country_code = optional_country_code(country_value)
        normalised_subdivision_code = optional_subdivision_code(subdivision_value)
        country = self.country(normalised_country_code)
        is_valid = True

        if normalised_country_code is not None and country is None:
            form.add_error(self.country_field, "Choose a valid country.")
            is_valid = False

        if normalised_subdivision_code is not None:
            if country is None:
                form.add_error(
                    self.subdivision_field,
                    "Choose a valid country before choosing a state or region.",
                )
                is_valid = False
            elif not self._subdivision_allowed(normalised_subdivision_code, country):
                form.add_error(
                    self.subdivision_field,
                    "Choose a valid state or region.",
                )
                is_valid = False

        normalised: NormalisedPhoneContact | None = None
        if number_value:
            if country is None:
                form.add_error(
                    self.country_field,
                    "Choose a valid country before entering a phone number.",
                )
                is_valid = False
            elif is_valid:
                try:
                    normalised = normalise_phone_contact(
                        number_value,
                        country_code=country.code,
                        subdivision_code=normalised_subdivision_code,
                        country_filter=self.country_filter,
                        subdivision_filter=self.subdivision_filter,
                    )
                except PhoneContactError as exc:
                    form.add_error(self._error_field(str(exc)), str(exc))
                    is_valid = False

        return PhoneContactValidation(
            country_code=(
                country.code if country is not None else normalised_country_code
            ),
            subdivision_code=normalised_subdivision_code,
            number=number_value or None,
            normalised=normalised,
            is_valid=is_valid,
        )

    def _subdivision_allowed(
        self,
        subdivision_code: str,
        country: CountryChoice,
    ) -> bool:
        return any(
            subdivision.code == subdivision_code
            for subdivision in self.subdivisions(country.code)
        )

    def _error_field(self, message: str) -> str:
        lower_message = message.casefold()
        if "country" in lower_message:
            return self.country_field
        if "subdivision" in lower_message or "state or region" in lower_message:
            return self.subdivision_field
        return self.phone_field


def form_control(form_or_type: Form | type[Form], control_name: str) -> Any:
    value = getattr(form_or_type, control_name)
    if isinstance(value, PhoneContactControl):
        return value
    raise PhoneContactError(
        f"Form control {control_name!r} is not a PhoneContactControl."
    )


def register_phone_contact_field_handlers(
    router: APIRouter,
    *,
    control: PhoneContactControl,
    form_factory: FormFactory,
    templates: TemplateFactory,
    target_id: str = "wybra-phone-contact-fields",
    dependencies: Sequence[Any] = (),
    form_route_name: str | None = None,
) -> None:
    from wybra.forms.rendering import render_phone_contact_fields

    handler = control.dependent_fields_handler()
    handler_path = _field_handler_path(
        router,
        handler,
        form_route_name=form_route_name,
    )
    registered_name = _registered_handler_name(control, handler)
    control._registered_handler_names[handler.name] = registered_name

    async def dependent_fields(request: Request) -> HTMLResponse:
        if handler.htmx and request.headers.get("HX-Request", "").lower() != "true":
            raise HTTPException(status_code=404)
        form = form_factory(request)
        country_code = request.query_params.get(control.country_field)
        return HTMLResponse(
            await render_phone_contact_fields(
                templates(request),
                form,
                subdivision_field=control.subdivision_field,
                phone_field=control.phone_field,
                phone_prefix=control.phone_prefix(country_code),
                target_id=target_id,
            )
        )

    router.add_api_route(
        handler_path,
        dependent_fields,
        methods=sorted(handler.methods),
        name=registered_name,
        include_in_schema=handler.include_in_schema,
        dependencies=list(dependencies),
    )


def _registered_handler_name(
    control: PhoneContactControl,
    handler: FieldHandler,
) -> str:
    return f"{handler.name}:{id(control):x}"


def _field_handler_path(
    router: APIRouter,
    handler: FieldHandler,
    *,
    form_route_name: str | None,
) -> str:
    if form_route_name is None:
        return handler.path
    return _join_route_paths(_route_path(router, form_route_name), handler.path)


def _route_path(router: APIRouter, route_name: str) -> str:
    for route in router.routes:
        if getattr(route, "name", None) == route_name:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                return path
    raise PhoneContactError(f"Form route {route_name!r} is not registered.")


def _join_route_paths(base_path: str, relative_path: str) -> str:
    base = base_path.rstrip("/")
    relative = relative_path.lstrip("/")
    if not base:
        return f"/{relative}"
    if not relative:
        return base
    return f"{base}/{relative}"


def country_choices(
    *,
    country_filter: CountryFilter | None = None,
) -> tuple[CountryChoice, ...]:
    countries: list[CountryChoice] = []
    for country in pycountry.countries:
        code = getattr(country, "alpha_2", None)
        name = getattr(country, "name", None)
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        choice = CountryChoice(
            code=code,
            name=name,
            dial_prefix=dial_prefix(code),
            flag=country_flag(code),
        )
        if country_filter is None or country_filter(choice):
            countries.append(choice)
    return tuple(sorted(countries, key=lambda country: country.name.casefold()))


def subdivision_choices(
    country_code: str,
    *,
    country_filter: CountryFilter | None = None,
    subdivision_filter: SubdivisionFilter | None = None,
) -> tuple[SubdivisionChoice, ...]:
    country = _filtered_country(country_code, country_filter=country_filter)
    subdivisions = pycountry.subdivisions.get(country_code=country.code)
    choices: list[SubdivisionChoice] = []
    for subdivision in subdivisions:
        choice = SubdivisionChoice(
            code=subdivision.code,
            name=subdivision.name,
            country_code=country.code,
        )
        if subdivision_filter is None or subdivision_filter(choice, country):
            choices.append(choice)
    return tuple(sorted(choices, key=lambda subdivision: subdivision.name.casefold()))


def normalise_phone_contact(
    number: str,
    *,
    country_code: str | None,
    subdivision_code: str | None = None,
    country_filter: CountryFilter | None = None,
    subdivision_filter: SubdivisionFilter | None = None,
) -> NormalisedPhoneContact:
    phone_number = phone_number_value(number)
    country = _filtered_country(country_code, country_filter=country_filter)
    normalised_subdivision_code = optional_subdivision_code(subdivision_code)
    if normalised_subdivision_code is not None and not any(
        subdivision.code == normalised_subdivision_code
        for subdivision in subdivision_choices(
            country.code,
            country_filter=country_filter,
            subdivision_filter=subdivision_filter,
        )
    ):
        raise PhoneContactError("Choose a valid state or region.")
    try:
        parsed = phonenumbers.parse(phone_number, country.code)
    except NumberParseException as exc:
        raise PhoneContactError("Phone contact number is invalid.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneContactError("Phone contact number is invalid.")

    resolved_country_code = phonenumbers.region_code_for_number(parsed)
    resolved_country = _filtered_country(
        resolved_country_code or country.code,
        country_filter=country_filter,
    )
    number_type = phonenumbers.number_type(parsed)
    return NormalisedPhoneContact(
        country_code=resolved_country.code,
        subdivision_code=normalised_subdivision_code,
        normalised_number=phonenumbers.format_number(parsed, PhoneNumberFormat.E164),
        number_type=phone_number_type_name(number_type),
        sms_capable=is_sms_capable(number_type),
    )


def country_flag(country_code: str) -> str:
    code = country_code_value(country_code)
    return "".join(chr(0x1F1E6 + ord(character) - ord("A")) for character in code)


def dial_prefix(country_code: str) -> str:
    phone_country_code = phonenumbers.country_code_for_region(
        country_code_value(country_code)
    )
    return f"+{phone_country_code}" if phone_country_code else ""


def phone_number_value(value: str) -> str:
    if not isinstance(value, str):
        raise PhoneContactError("Phone contact number must be text.")
    phone_number = value.strip()
    if not phone_number:
        raise PhoneContactError("Phone contact number must not be blank.")
    return phone_number


def optional_country_code(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return country_code_value(text)


def country_code_value(value: str) -> str:
    if not isinstance(value, str):
        raise PhoneContactError("Country code must be text.")
    country_code = value.strip().upper()
    if len(country_code) != 2 or pycountry.countries.get(alpha_2=country_code) is None:
        raise PhoneContactError("Choose a valid country.")
    return country_code


def optional_subdivision_code(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PhoneContactError("Subdivision code must be text.")
    subdivision_code = value.strip().upper()
    return subdivision_code or None


def phone_number_type_name(number_type: int) -> str:
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


def is_sms_capable(number_type: int) -> bool:
    return number_type in {
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }


def _filtered_country(
    country_code: str | None,
    *,
    country_filter: CountryFilter | None,
) -> CountryChoice:
    normalised_country_code = optional_country_code(country_code)
    if normalised_country_code is None:
        raise PhoneContactError("Choose a valid country.")
    for country in country_choices(country_filter=country_filter):
        if country.code == normalised_country_code:
            return country
    raise PhoneContactError("Choose a valid country.")


__all__ = (
    "CountryChoice",
    "CountryFilter",
    "FieldHandler",
    "FormFactory",
    "NormalisedPhoneContact",
    "PhoneContactControl",
    "PhoneContactError",
    "PhoneContactValidation",
    "SubdivisionChoice",
    "SubdivisionFilter",
    "TemplateFactory",
    "country_choices",
    "country_flag",
    "dial_prefix",
    "field_handler",
    "form_control",
    "normalise_phone_contact",
    "phone_number_type_name",
    "register_phone_contact_field_handlers",
    "subdivision_choices",
)
