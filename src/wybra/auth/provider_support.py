from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.mfa.storage import SqlAlchemyWebAuthnCredentialStore
from wybra.auth.mfa.webauthn import passkeys_effectively_enabled
from wybra.auth.models import User
from wybra.auth.provider_credentials import (
    ProviderCredentialStore,
    provider_credential_store,
)
from wybra.core.exceptions import ConfigurationError
from wybra.providers.capabilities import ProvidersCapability
from wybra.providers.descriptors import (
    ProviderAuthDescriptor,
    provider_auth_descriptors,
)
from wybra.providers.descriptors import (
    provider_label as provider_label,
)
from wybra.providers.settings import ProviderSettings
from wybra.site import SiteCapabilityError, get_site


def enabled_provider(
    request: Request,
    descriptor: ProviderAuthDescriptor,
) -> ProviderSettings | None:
    try:
        providers = get_site(request.app).optional_capability(ProvidersCapability)
    except SiteCapabilityError:
        return None
    if providers is None:
        return None
    try:
        provider = providers.settings.provider(descriptor.name)
    except ConfigurationError:
        return None
    if not provider.enabled:
        return None
    try:
        descriptor.settings_factory(provider)
    except ConfigurationError:
        return None
    return provider


def provider_login_path(
    request: Request,
    descriptor: ProviderAuthDescriptor,
    *,
    return_to: str | None = None,
) -> str | None:
    from wybra.auth.routes.paths import normalise_return_to, optional_route_path

    route_path = optional_route_path(request, descriptor.login_route_name)
    if route_path is None or enabled_provider(request, descriptor) is None:
        return None
    query = urlencode({"return_to": normalise_return_to(return_to)})
    return f"{route_path}?{query}"


def provider_link_path(
    request: Request,
    descriptor: ProviderAuthDescriptor,
    *,
    return_to: str | None = None,
) -> str | None:
    from wybra.auth.routes.paths import normalise_return_to, optional_route_path

    route_path = optional_route_path(request, descriptor.link_route_name)
    if route_path is None or enabled_provider(request, descriptor) is None:
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


def provider_security_options(
    request: Request,
    *,
    return_to: str | None,
) -> tuple[dict[str, str], ...]:
    from wybra.auth.routes.paths import optional_route_path

    options: list[dict[str, str]] = []
    for descriptor in provider_auth_descriptors():
        link_path = provider_link_path(request, descriptor, return_to=return_to)
        unlink_path = optional_route_path(
            request,
            descriptor.security_unlink_route_name,
        )
        if link_path is None or unlink_path is None:
            continue
        options.append(
            {
                "name": descriptor.name,
                "label": descriptor.label,
                "link_path": link_path,
                "unlink_path": unlink_path,
            }
        )
    return tuple(options)


def provider_login_options(
    request: Request,
    *,
    return_to: str | None,
) -> tuple[dict[str, str], ...]:
    options: list[dict[str, str]] = []
    for descriptor in provider_auth_descriptors():
        login_path = provider_login_path(request, descriptor, return_to=return_to)
        if login_path is None:
            continue
        options.append(
            {
                "name": descriptor.name,
                "label": descriptor.label,
                "login_path": login_path,
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
    exclude_passkey_id: str | UUID | None = None,
) -> bool:
    if not exclude_password and local_password_login_usable(user):
        return True
    if await user_has_usable_passkey(
        request,
        session,
        user,
        exclude_passkey_id=exclude_passkey_id,
    ):
        return True
    store = provider_credential_store_from_request(request, session)
    return await store.user_has_enabled_provider_link(
        user.id,
        provider_names=usable_provider_names(request),
        exclude_provider_id=exclude_provider_id,
    )


def usable_provider_names(request: Request) -> tuple[str, ...]:
    names: list[str] = []
    for descriptor in provider_auth_descriptors():
        if enabled_provider(request, descriptor) is not None:
            names.append(descriptor.name)
    return tuple(names)


async def user_has_usable_passkey(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    exclude_passkey_id: str | UUID | None = None,
) -> bool:
    options = getattr(request.app.state.auth_settings, "identity_options", None)
    if options is None or not passkeys_effectively_enabled(options):
        return False

    store = SqlAlchemyWebAuthnCredentialStore(session)
    return (
        await store.count_active_webauthn_credentials(
            str(user.id),
            exclude_row_id=exclude_passkey_id,
        )
        > 0
    )


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
