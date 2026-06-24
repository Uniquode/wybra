from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from wybra.auth import login_required
from wybra.db import DatabaseCapability
from wybra.forms import (
    FormsCapability,
    forms_rendering_context,
    request_form_data,
    validate_csrf,
)
from wybra.profile.capabilities import ProfileCapability
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.forms import ProfileEditForm, profile_form_values
from wybra.profile.models import UserPhoneContact
from wybra.profile.phone import country_choices
from wybra.profile.settings import ProfileSettings
from wybra.profile.utils import normalise_form_text, normalise_return_to
from wybra.site import get_site
from wybra.template import TemplateCapability

PROFILE_EDIT_TEMPLATE = "profile/pages/edit.html"
LOGIN_REQUIRED = Depends(login_required)

profile_router = APIRouter(dependencies=[Depends(validate_csrf)])


@profile_router.api_route(
    "/profile",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="profile:edit",
)
async def edit_profile(
    request: Request,
    user: Any = LOGIN_REQUIRED,
) -> Response:
    site = get_site(request.app)
    settings = ProfileSettings.load_settings(site.config)
    forms = site.require_capability(FormsCapability)
    templates = site.require_capability(TemplateCapability)
    profile_capability = site.require_capability(ProfileCapability)
    database = site.require_capability(DatabaseCapability)
    default_return_to = str(request.url_for("profile:edit"))
    return_to = _normalise_return_to(
        request.query_params.get("return_to"),
        default=default_return_to,
    )

    form_error: str | None = None
    profile_form: ProfileEditForm | None = None
    current_phone_contact: UserPhoneContact | None = None
    async with database.session() as session:
        profile = await profile_capability.get_profile(session, user.id)
        phone_contacts = await profile_capability.list_phone_contacts(session, user.id)
        current_phone_contact = _current_phone_contact(phone_contacts)
        if request.method == "POST":
            try:
                form_data = await request_form_data(request)
                return_to = _normalise_return_to(
                    _form_value(form_data, "return_to"),
                    default=return_to,
                )
                profile_form = ProfileEditForm(
                    settings=settings,
                    values=_profile_form_values(
                        profile,
                        return_to=return_to,
                        phone_contact=current_phone_contact,
                    ),
                )
                profile_form.parse(form_data)
                if profile_form.is_valid():
                    profile_data = profile_form.profile_field_data()
                    if profile_data and (
                        profile is not None or _has_non_empty_profile_data(profile_data)
                    ):
                        await profile_capability.save_profile_fields(
                            session,
                            user.id,
                            profile_data,
                            settings=settings,
                        )
                    if profile_form.has_phone_contact_data():
                        phone_contact = profile_form.phone_contact_data()
                        await profile_capability.save_phone_contact(
                            session,
                            user.id,
                            number=phone_contact["number"] or "",
                            country_code=phone_contact["country_code"],
                            subdivision_code=phone_contact["subdivision_code"],
                        )
                    await session.commit()
                    return RedirectResponse(
                        url=return_to,
                        status_code=303,
                    )
            except ProfileInputError as exc:
                await session.rollback()
                if profile_form is None:
                    profile_form = ProfileEditForm(
                        settings=settings,
                        values=_profile_form_values(
                            profile,
                            return_to=return_to,
                            phone_contact=current_phone_contact,
                        ),
                    )
                profile_form.add_error(None, str(exc))
                form_error = str(exc)

        if profile_form is None:
            profile_form = ProfileEditForm(
                settings=settings,
                values=_profile_form_values(
                    profile,
                    return_to=return_to,
                    phone_contact=current_phone_contact,
                ),
            )

    context = {
        **forms.token_context(request),
        "profile_form": profile_form,
        "editable_fields": settings.editable_fields,
        "phone_contacts": phone_contacts,
        "phone_contact_status": _phone_contact_status(current_phone_contact),
        "phone_prefix": _phone_prefix_for_country(
            normalise_form_text(profile_form.values.get("phone_country_code"))
        ),
        "phone_prefix_path": str(request.url_for("profile:phone-fields")),
        "form_error": form_error,
    }
    return templates.render_page(
        request,
        PROFILE_EDIT_TEMPLATE,
        context,
        status_code=400 if not profile_form.is_valid() else 200,
    )


@profile_router.get(
    "/profile/phone-fields",
    include_in_schema=False,
    name="profile:phone-fields",
)
async def phone_fields(
    request: Request,
    user: Any = LOGIN_REQUIRED,
) -> Response:
    del user
    country_code = request.query_params.get("phone_country_code")
    site = get_site(request.app)
    settings = ProfileSettings.load_settings(site.config)
    templates = site.require_capability(TemplateCapability)
    profile_form = ProfileEditForm(
        settings=settings,
        values={"phone_country_code": country_code or ""},
    )
    return HTMLResponse(
        templates.render_template(
            "profile/components/phone_fields.html",
            {
                **forms_rendering_context(templates),
                "phone_contact_status": None,
                "phone_prefix": _phone_prefix_for_country(country_code),
                "profile_form": profile_form,
            },
        )
    )


module_routers = {"profile": profile_router}


def _profile_form_values(
    profile: Any,
    *,
    return_to: str,
    phone_contact: UserPhoneContact | None = None,
) -> dict[str, object]:
    values = profile_form_values(profile)
    values["return_to"] = return_to
    if phone_contact is not None:
        values.update(
            {
                "phone_country_code": phone_contact.country_code,
                "phone_subdivision_code": phone_contact.subdivision_code or "",
                "phone_number": phone_contact.normalised_number,
            }
        )
    return values


def _form_value(form_data: Any, name: str, default: str = "") -> str:
    value = form_data.get(name, default)
    return value if isinstance(value, str) else default


def _phone_prefix_for_country(country_code: str | None) -> str:
    if not country_code:
        return ""
    normalised_country_code = country_code.strip().upper()
    for country in country_choices():
        if country.code == normalised_country_code:
            return f"{country.flag} {country.dial_prefix}".strip()
    return ""


def _current_phone_contact(
    phone_contacts: tuple[UserPhoneContact, ...],
) -> UserPhoneContact | None:
    return phone_contacts[0] if phone_contacts else None


def _phone_contact_status(phone_contact: UserPhoneContact | None) -> str | None:
    if phone_contact is None:
        return None
    return "Verified" if phone_contact.verified_at else "Not verified"


def _has_non_empty_profile_data(data: dict[str, object]) -> bool:
    return any(value is not None for value in data.values())


def _normalise_return_to(value: str | None, *, default: str) -> str:
    return normalise_return_to(value, default=default)


__all__ = (
    "PROFILE_EDIT_TEMPLATE",
    "edit_profile",
    "module_routers",
    "phone_fields",
    "profile_router",
)
