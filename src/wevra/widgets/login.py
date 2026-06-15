from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.routing import NoMatchFound

from wevra.auth.capabilities import AuthCapability
from wevra.auth.profile import ProfileImage, profile_image_for_user
from wevra.site import SiteCapabilityError, get_site

LOGIN_TEMPLATE = "components/login_control.html"
LOGIN_ROUTE_NAME = "auth:login"
LOGOUT_ROUTE_NAME = "auth:logout"


@dataclass(frozen=True, slots=True)
class LoginWidgetState:
    authenticated: bool
    login_path: str | None
    logout_path: str | None
    profile_image: ProfileImage | None = None


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
        profile_image=profile_image_for_user(user),
    )


def _auth_capability(request: Any) -> AuthCapability | None:
    try:
        site = get_site(request.app)
    except SiteCapabilityError:
        return None
    if not site.has_capability(AuthCapability):
        return None
    return site.require_capability(AuthCapability)


def _route_path(request: Any, route_name: str) -> str | None:
    try:
        return str(request.app.url_path_for(route_name))
    except NoMatchFound:
        return None


__all__ = (
    "LOGIN_ROUTE_NAME",
    "LOGIN_TEMPLATE",
    "LOGOUT_ROUTE_NAME",
    "LoginWidgetState",
    "login_widget_state",
)
