from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import Response
from fastapi_users import FastAPIUsers
from sqlalchemy.exc import SQLAlchemyError

from wybra.auth.delivery import NullIdentityDelivery
from wybra.auth.models import User
from wybra.auth.sessions import (
    clear_marked_session_cookie,
    create_fastapi_users,
    mark_session_cookie_for_clearing,
)
from wybra.auth.sessions import (
    optional_current_user as _optional_current_user,
)
from wybra.auth.sessions import (
    require_anonymous_user as _require_anonymous_user,
)
from wybra.auth.sessions import (
    require_current_user as _require_current_user,
)
from wybra.auth.settings import AuthSettings
from wybra.db.capabilities import DatabaseCapability
from wybra.site import Site, get_site
from wybra.site_config import app_config_from_site

logger = logging.getLogger(__name__)
OptionalCurrentUserDependency = Callable[[Request], Awaitable[User | None]]
RequiredCurrentUserDependency = Callable[[Request], Awaitable[User]]
AnonymousUserDependency = Callable[[Request], Awaitable[None]]


@runtime_checkable
class AuthCapability(Protocol):
    """Public auth capability exposed through ``Site``."""

    @property
    def settings(self) -> AuthSettings: ...

    @property
    def fastapi_users(self) -> FastAPIUsers[User, uuid.UUID]: ...

    @property
    def optional_current_user(self) -> OptionalCurrentUserDependency: ...

    @property
    def login_required(self) -> RequiredCurrentUserDependency: ...

    @property
    def anonymous_required(self) -> AnonymousUserDependency: ...


@dataclass(frozen=True, slots=True)
class SiteAuthCapability:
    settings: AuthSettings
    fastapi_users: FastAPIUsers[User, uuid.UUID]

    @classmethod
    def from_settings(cls, settings: AuthSettings) -> SiteAuthCapability:
        return cls(
            settings=settings,
            fastapi_users=create_fastapi_users(settings.identity_options),
        )

    @property
    def optional_current_user(self) -> OptionalCurrentUserDependency:
        return _safe_optional_current_user

    @property
    def login_required(self) -> RequiredCurrentUserDependency:
        return _require_current_user

    @property
    def anonymous_required(self) -> AnonymousUserDependency:
        return _require_anonymous_user


async def optional_current_user(request: Request) -> User | None:
    return await _auth_capability_from_request(request).optional_current_user(request)


async def login_required(request: Request) -> User:
    return await _auth_capability_from_request(request).login_required(request)


async def anonymous_required(request: Request) -> None:
    return await _auth_capability_from_request(request).anonymous_required(request)


async def setup_site(site: Site) -> None:
    app_config = app_config_from_site(site)
    settings = AuthSettings.load_settings(
        site.config,
        app_config=app_config,
        deployment_environment=app_config.deployment_environment,
    )
    capability = SiteAuthCapability.from_settings(settings)

    site.app.state.auth_settings = settings
    site.app.state.identity_delivery = NullIdentityDelivery()
    site.app.state.fastapi_users = capability.fastapi_users
    site.provide_capability(AuthCapability, capability)
    _register_session_cookie_cleanup_middleware(site, settings)


async def post_setup_site(site: Site) -> None:
    site.require_capability(DatabaseCapability)


def _register_session_cookie_cleanup_middleware(
    site: Site,
    settings: AuthSettings,
) -> None:
    if getattr(site.app.state, "identity_session_cookie_cleanup_registered", False):
        return

    @site.app.middleware("http")
    async def session_cookie_cleanup_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        clear_marked_session_cookie(response, request, settings.identity_options)
        return response

    site.app.state.identity_session_cookie_cleanup_registered = True


def _auth_capability_from_request(request: Request) -> AuthCapability:
    return get_site(request.app).require_capability(AuthCapability)


async def _safe_optional_current_user(request: Request) -> User | None:
    try:
        return await _optional_current_user(request)
    except SQLAlchemyError as exc:
        logger.warning(
            "Auth optional current user lookup failed.",
            extra={
                "request_path": getattr(getattr(request, "url", None), "path", None),
                "error_type": type(exc).__name__,
                "auth_context": "optional_current_user",
                "auth_action": "clear_session_cookie_and_treat_as_anonymous",
            },
            exc_info=True,
        )
        mark_session_cookie_for_clearing(request)
        return None
