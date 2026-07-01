from collections.abc import Callable
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.models import User
from wybra.auth.provider_credentials import (
    ProviderCredentialStore,
    provider_credential_store,
)
from wybra.auth.routes.paths import normalise_return_to, optional_route_path
from wybra.core.exceptions import ConfigurationError
from wybra.providers.capabilities import ProvidersCapability
from wybra.providers.github import (
    GITHUB_PROVIDER_NAME,
    github_oauth_settings_from_provider,
)
from wybra.providers.google import (
    GOOGLE_PROVIDER_NAME,
    google_oauth_settings_from_provider,
)
from wybra.providers.settings import ProviderSettings
from wybra.site import SiteCapabilityError, get_site


def enabled_google_provider(request: Request) -> ProviderSettings | None:
    return _enabled_provider(
        request,
        provider_name=GOOGLE_PROVIDER_NAME,
        settings_factory=google_oauth_settings_from_provider,
    )


def enabled_github_provider(request: Request) -> ProviderSettings | None:
    return _enabled_provider(
        request,
        provider_name=GITHUB_PROVIDER_NAME,
        settings_factory=github_oauth_settings_from_provider,
    )


def _enabled_provider(
    request: Request,
    *,
    provider_name: str,
    settings_factory: Callable[[ProviderSettings], object],
) -> ProviderSettings | None:
    try:
        providers = get_site(request.app).optional_capability(ProvidersCapability)
    except SiteCapabilityError:
        return None
    if providers is None:
        return None
    try:
        provider = providers.settings.provider(provider_name)
    except ConfigurationError:
        return None
    if not provider.enabled:
        return None
    try:
        settings_factory(provider)
    except ConfigurationError:
        return None
    return provider


def google_login_path(request: Request, *, return_to: str | None = None) -> str | None:
    route_path = optional_route_path(request, "auth:google-login")
    if route_path is None or enabled_google_provider(request) is None:
        return None
    query = urlencode({"return_to": normalise_return_to(return_to)})
    return f"{route_path}?{query}"


def google_link_path(request: Request, *, return_to: str | None = None) -> str | None:
    route_path = optional_route_path(request, "auth:google-link")
    if route_path is None or enabled_google_provider(request) is None:
        return None
    security_path = (
        optional_route_path(request, "auth:security")
        or optional_route_path(request, "auth:account")
        or "/"
    )
    query = urlencode(
        {"return_to": normalise_return_to(return_to, default=security_path)}
    )
    return f"{route_path}?{query}"


def github_login_path(request: Request, *, return_to: str | None = None) -> str | None:
    route_path = optional_route_path(request, "auth:github-login")
    if route_path is None or enabled_github_provider(request) is None:
        return None
    query = urlencode({"return_to": normalise_return_to(return_to)})
    return f"{route_path}?{query}"


def github_link_path(request: Request, *, return_to: str | None = None) -> str | None:
    route_path = optional_route_path(request, "auth:github-link")
    if route_path is None or enabled_github_provider(request) is None:
        return None
    security_path = (
        optional_route_path(request, "auth:security")
        or optional_route_path(request, "auth:account")
        or "/"
    )
    query = urlencode(
        {"return_to": normalise_return_to(return_to, default=security_path)}
    )
    return f"{route_path}?{query}"


def provider_login_options(
    request: Request,
    *,
    return_to: str | None,
) -> tuple[dict[str, str], ...]:
    options: list[dict[str, str]] = []
    google_path = google_login_path(request, return_to=return_to)
    if google_path is not None:
        options.append(
            {
                "name": GOOGLE_PROVIDER_NAME,
                "label": "Google",
                "login_path": google_path,
            }
        )
    github_path = github_login_path(request, return_to=return_to)
    if github_path is not None:
        options.append(
            {
                "name": GITHUB_PROVIDER_NAME,
                "label": "GitHub",
                "login_path": github_path,
            }
        )
    return tuple(options)


async def user_has_usable_account_sign_in(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    exclude_password: bool = False,
    exclude_provider_id: str | UUID | None = None,
) -> bool:
    if not exclude_password and local_password_login_usable(user):
        return True
    store = provider_credential_store_from_request(request, session)
    return await store.user_has_enabled_provider_link(
        user.id,
        provider_names=usable_provider_names(request),
        exclude_provider_id=exclude_provider_id,
    )


def usable_provider_names(request: Request) -> tuple[str, ...]:
    names: list[str] = []
    if enabled_google_provider(request) is not None:
        names.append(GOOGLE_PROVIDER_NAME)
    if enabled_github_provider(request) is not None:
        names.append(GITHUB_PROVIDER_NAME)
    return tuple(names)


def local_password_login_usable(user: object) -> bool:
    return bool(
        getattr(user, "password_login_enabled", True)
        and getattr(user, "hashed_password", None)
    )


def provider_credential_store_from_request(
    request: Request,
    session: AsyncSession,
) -> ProviderCredentialStore:
    return provider_credential_store(
        session,
        getattr(request.app.state, "secret_envelope_service", None),
    )
