from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth import login_required
from wybra.forms import (
    FormsCapability,
    register_phone_contact_field_handlers,
    request_form_data,
    validate_csrf,
)
from wybra.profile.capabilities import ProfileCapability
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.forms import ProfileEditForm, profile_form_values
from wybra.profile.models import UserPhoneContact
from wybra.profile.settings import ProfileSettings
from wybra.profile.utils import normalise_return_to
from wybra.site import get_site
from wybra.template import TemplateCapability

PROFILE_EDIT_TEMPLATE = "profile/pages/edit.html"
PROFILE_EDIT_ROUTE_NAME = "profile:edit"
LOGIN_REQUIRED = Depends(login_required)

profile_router = APIRouter(dependencies=[Depends(validate_csrf)])


@profile_router.api_route(
    "/profile",
    methods=["GET", "POST"],
    include_in_schema=False,
    name=PROFILE_EDIT_ROUTE_NAME,
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
    default_return_to = str(request.url_for(PROFILE_EDIT_ROUTE_NAME))
    return_to = _normalise_return_to(
        request.query_params.get("return_to"),
        default=default_return_to,
    )

    form_error: str | None = None
    profile_form: ProfileEditForm | None = None
    current_phone_contact: UserPhoneContact | None = None
    profile = await profile_capability.get_profile(user.id)
    phone_contacts = await profile_capability.list_phone_contacts(user.id)
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
            await profile_form.parse(form_data)
            if profile_form.is_valid():
                profile_data = profile_form.profile_field_data()
                phone_contact = (
                    profile_form.phone_contact_data()
                    if profile_form.has_phone_contact_data()
                    else None
                )
                if (
                    phone_contact is not None
                    or profile is not None
                    or _has_non_empty_profile_data(profile_data)
                ):
                    await profile_capability.save_profile_edit(
                        user.id,
                        profile_data,
                        settings=settings,
                        phone_contact=phone_contact,
                    )
                return RedirectResponse(
                    url=return_to,
                    status_code=303,
                )
        except ProfileInputError as exc:
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
        "form_error": form_error,
    }
    return templates.render_page(
        request,
        PROFILE_EDIT_TEMPLATE,
        context,
        status_code=400 if not profile_form.is_valid() else 200,
    )


def _profile_phone_contact_form(request: Request) -> ProfileEditForm:
    site = get_site(request.app)
    settings = ProfileSettings.load_settings(site.config)
    country_code = request.query_params.get(
        ProfileEditForm.phone_contact.country_field,
    )
    subdivision_code = request.query_params.get(
        ProfileEditForm.phone_contact.subdivision_field,
    )
    phone_number = request.query_params.get(ProfileEditForm.phone_contact.phone_field)
    return ProfileEditForm(
        settings=settings,
        values={
            "phone_country_code": country_code or "",
            "phone_subdivision_code": subdivision_code or "",
            "phone_number": phone_number or "",
        },
    )


def _template_capability(request: Request) -> TemplateCapability:
    return get_site(request.app).require_capability(TemplateCapability)


register_phone_contact_field_handlers(
    profile_router,
    control=ProfileEditForm.phone_contact,
    form_factory=_profile_phone_contact_form,
    templates=_template_capability,
    target_id="profile-phone-fields",
    dependencies=(LOGIN_REQUIRED,),
    form_route_name=PROFILE_EDIT_ROUTE_NAME,
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
    "PROFILE_EDIT_ROUTE_NAME",
    "edit_profile",
    "module_routers",
    "profile_router",
)
