from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth import login_required
from wybra.db import DatabaseCapability
from wybra.forms import FormsCapability, request_form_data, validate_csrf
from wybra.profile.capabilities import ProfileCapability
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.phone import country_choices, subdivision_choices
from wybra.profile.settings import ProfileSettings
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

    form_error: str | None = None
    async with database.session() as session:
        if request.method == "POST":
            try:
                form_data = await request_form_data(request)
                async with session.begin():
                    await profile_capability.save_profile_fields(
                        session,
                        user.id,
                        _profile_field_submission(form_data),
                        settings=settings,
                    )
                    if _has_phone_submission(form_data):
                        await profile_capability.save_phone_contact(
                            session,
                            user.id,
                            number=_form_text(form_data, "phone_number"),
                            country_code=_form_text(form_data, "phone_country_code"),
                            subdivision_code=_form_text(
                                form_data,
                                "phone_subdivision_code",
                            )
                            or None,
                        )
                return RedirectResponse(
                    url=str(request.url_for("profile:edit")),
                    status_code=303,
                )
            except ProfileInputError as exc:
                await session.rollback()
                form_error = str(exc)

        profile = await profile_capability.get_profile(session, user.id)
        phone_contacts = await profile_capability.list_phone_contacts(session, user.id)

    context = {
        **forms.token_context(request),
        "user": user,
        "profile": profile,
        "profile_settings": settings,
        "editable_fields": settings.editable_fields,
        "field_values": _field_values(profile),
        "phone_contacts": phone_contacts,
        "country_choices": country_choices(),
        "subdivision_choices": subdivision_choices,
        "form_error": form_error,
    }
    return templates.render_page(
        request,
        PROFILE_EDIT_TEMPLATE,
        context,
        status_code=400 if form_error else 200,
    )


def _profile_field_submission(form_data: Any) -> dict[str, object]:
    values: dict[str, object] = {}
    _add_form_text(values, form_data, "preferred_name")
    _add_form_text(values, form_data, "display_name")
    _add_form_text(values, form_data, "bio")
    pronoun_pair = _pronoun_pair(_form_text(form_data, "pronoun_pair"))
    if pronoun_pair is not None:
        values["pronouns"] = pronoun_pair
    profile_link = _form_text(form_data, "profile_link_website")
    if profile_link:
        values["profile_links"] = {"website": profile_link}
    return values


def _field_values(profile: Any) -> dict[str, object]:
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


def _pronoun_pair(value: str) -> dict[str, str] | None:
    if not value:
        return None
    direct, separator, possessive = value.partition("|")
    if not separator:
        raise ProfileInputError("Pronouns must use a configured pair.")
    return {"direct": direct, "possessive": possessive}


def _profile_pronoun_pair(pronouns: dict[str, object]) -> str:
    direct = pronouns.get("direct")
    possessive = pronouns.get("possessive")
    if isinstance(direct, str) and isinstance(possessive, str):
        return f"{direct}|{possessive}"
    return ""


def _add_form_text(values: dict[str, object], form_data: Any, name: str) -> None:
    value = _form_text(form_data, name)
    if value:
        values[name] = value


def _form_text(form_data: Any, name: str) -> str:
    value = form_data.get(name, "")
    return value if isinstance(value, str) else ""


def _has_phone_submission(form_data: Any) -> bool:
    return bool(_form_text(form_data, "phone_number"))


module_routers = {"profile": profile_router}

__all__ = (
    "PROFILE_EDIT_TEMPLATE",
    "edit_profile",
    "module_routers",
    "profile_router",
)
