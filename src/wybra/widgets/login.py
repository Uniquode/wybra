from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.routing import NoMatchFound

from wybra.auth.capabilities import AuthCapability
from wybra.profile import ProfileCapability, ProfileImage
from wybra.site import SiteCapabilityError, get_site

logger = logging.getLogger(__name__)
LOGIN_TEMPLATE = "components/login_control.html"
LOGIN_ROUTE_NAME = "auth:login"
LOGOUT_ROUTE_NAME = "auth:logout"
PROFILE_EDIT_ROUTE_NAME = "profile:edit"


@dataclass(frozen=True, slots=True)
class LoginWidgetState:
    authenticated: bool
    login_path: str | None
    logout_path: str | None
    profile_image: ProfileImage | None = None
    profile_path: str | None = None


async def login_widget_state(request: Any) -> LoginWidgetState | None:
    capability = _auth_capability(request)
    if capability is None:
        return None

    login_path = _route_path(request, LOGIN_ROUTE_NAME)
    if login_path is None:
        return None

    user = await capability.optional_current_user(request)
    if user is None:
        return LoginWidgetState(
            authenticated=False,
            login_path=login_path,
            logout_path=None,
        )

    logout_path = _route_path(request, LOGOUT_ROUTE_NAME)
    if logout_path is None:
        return None

    return LoginWidgetState(
        authenticated=True,
        login_path=None,
        logout_path=logout_path,
        profile_image=await _profile_image(request, user),
        profile_path=_profile_path(request),
    )


def _auth_capability(request: Any) -> AuthCapability | None:
    try:
        site = get_site(request.app)
    except SiteCapabilityError:
        return None

    return site.capability_proxy(AuthCapability).optional()


async def _profile_image(request: Any, user: Any) -> ProfileImage | None:
    try:
        profile_capability = get_site(request.app).capability_proxy(ProfileCapability)
    except SiteCapabilityError:
        return None

    capability = profile_capability.optional()
    if capability is None:
        return None

    return await capability.profile_image_for_user(user)


def _route_path(request: Any, route_name: str) -> str | None:
    try:
        return str(request.app.url_path_for(route_name))
    except NoMatchFound:
        return None


def _profile_path(request: Any) -> str | None:
    settings = getattr(request.app.state, "widgets_settings", None)
    if settings is None:
        logger.warning("widgets_settings missing; profile avatar navigation disabled.")
        return None
    if not getattr(settings, "default_profile_avatar_navigation", False):
        return None
    return _route_path(request, PROFILE_EDIT_ROUTE_NAME)


__all__ = (
    "LOGIN_ROUTE_NAME",
    "LOGIN_TEMPLATE",
    "LOGOUT_ROUTE_NAME",
    "LoginWidgetState",
    "PROFILE_EDIT_ROUTE_NAME",
    "login_widget_state",
)
