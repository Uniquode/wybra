from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, Final, Protocol, cast

from fastapi import HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users.exceptions import (
    FastAPIUsersException,
    InvalidID,
    InvalidPasswordException,
    InvalidVerifyToken,
    UserAlreadyExists,
    UserAlreadyVerified,
    UserNotExists,
)
from fastapi_users.jwt import decode_jwt
from jwt import PyJWTError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wevra.auth.accounts.manager import (
    UserManager,
    create_user_manager,
    public_password_error_type,
)
from wevra.auth.accounts.schemas import UserCreate
from wevra.auth.authorisation import is_user_effectively_active
from wevra.auth.delivery import IdentityDelivery, NullIdentityDelivery
from wevra.auth.models import AccessToken, User
from wevra.auth.options import IdentityOptions
from wevra.auth.persistence import (
    create_database_strategy,
    delete_session_token_by_value,
)
from wevra.auth.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_ALREADY_VERIFIED,
    ERROR_IDENTITY_CHANGED,
    ERROR_INACTIVE_USER,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_TOKEN,
    ERROR_POLICY_DISABLED,
    ERROR_TOKEN_REJECTED,
    Result,
)
from wevra.auth.timestamps import current_timestamp

_CURRENT_USER_CACHE_TOKEN_ATTR = "identity_current_user_token"
_CURRENT_USER_CACHE_VALUE_ATTR = "identity_current_user"
_CURRENT_USER_CACHE_MISSING = object()
# Cookie security trust model:
# - The ASGI request scheme is authoritative only after the server or middleware
#   has normalised headers from trusted proxies.
# - Scheme aliases translate already-normalised secure ASGI schemes into
#   HTTP(S) cookie semantics; they do not inspect raw client headers.
# - Raw forwarding headers are advisory diagnostics only and never mark cookies
#   secure by themselves.
SECURE_COOKIE_SCHEMES: Final = frozenset({"https"})
SECURE_SCHEME_ALIASES: Final = {
    "wss": "https",
}
FORWARDED_PROTO_HEADER: Final = "x-forwarded-proto"
FORWARDED_HEADER: Final = "forwarded"
# Process-scoped warning suppression. A race can only emit a duplicate warning,
# which is acceptable for this diagnostic path.
_logged_forward_header_misconfig = False
logger = logging.getLogger(__name__)


class SupportsSessionFactory(Protocol):
    session_factory: async_sessionmaker[AsyncSession]


def _database_from_request(request: Request) -> SupportsSessionFactory:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise RuntimeError("Database is not configured on the application.")

    if not hasattr(database, "session_factory"):
        raise RuntimeError("Database session factory is not configured.")

    return cast(SupportsSessionFactory, database)


def _session_factory_from_request(
    request: Request,
) -> async_sessionmaker[AsyncSession]:
    session_factory = _database_from_request(request).session_factory
    if session_factory is None:
        raise RuntimeError("Database session factory is not configured.")

    return session_factory


def _identity_options_from_request(request: Request) -> IdentityOptions:
    options = getattr(request.app.state, "identity_options", None)
    if not isinstance(options, IdentityOptions):
        raise RuntimeError("Identity options are not configured on the application.")

    return options


def _delivery_from_request(request: Request) -> IdentityDelivery:
    delivery = getattr(request.app.state, "identity_delivery", None)
    if delivery is None:
        return NullIdentityDelivery()

    return delivery


def create_user_manager_dependency(
    options: IdentityOptions,
) -> Callable[[Request], AsyncIterator[UserManager]]:
    async def get_user_manager(request: Request) -> AsyncIterator[UserManager]:
        session_factory = _session_factory_from_request(request)
        async with session_factory() as session:
            yield create_user_manager(session, options, _delivery_from_request(request))

    return get_user_manager


def create_database_strategy_dependency(
    options: IdentityOptions,
) -> Callable[[Request], AsyncIterator[DatabaseStrategy[User, uuid.UUID, AccessToken]]]:
    async def get_database_strategy(
        request: Request,
    ) -> AsyncIterator[DatabaseStrategy[User, uuid.UUID, AccessToken]]:
        session_factory = _session_factory_from_request(request)
        async with session_factory() as session:
            yield create_database_strategy(session, options)

    return get_database_strategy


def create_authentication_backend(
    options: IdentityOptions,
) -> AuthenticationBackend[User, uuid.UUID]:
    transport = CookieTransport(
        cookie_name=options.session_cookie_name,
        cookie_max_age=options.session_lifetime_seconds,
        cookie_secure=options.session_cookie_force_secure,
        cookie_httponly=True,
        cookie_samesite="lax",
    )
    return AuthenticationBackend(
        name="session",
        transport=transport,
        get_strategy=create_database_strategy_dependency(options),
    )


def create_fastapi_users(options: IdentityOptions) -> FastAPIUsers[User, uuid.UUID]:
    return FastAPIUsers(
        create_user_manager_dependency(options),
        [create_authentication_backend(options)],
    )


def set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    options: IdentityOptions,
) -> None:
    response.set_cookie(
        options.session_cookie_name,
        token,
        max_age=options.session_lifetime_seconds,
        path="/",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=options.session_cookie_force_secure,
        ),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(
    response: Response,
    request: Request,
    options: IdentityOptions,
) -> None:
    response.set_cookie(
        options.session_cookie_name,
        "",
        max_age=0,
        path="/",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=options.session_cookie_force_secure,
        ),
        httponly=True,
        samesite="lax",
    )


def session_cookie_secure_for_request(
    request: Request,
    *,
    force_secure: bool = False,
) -> bool:
    """Return whether browser session cookies should be marked secure.

    Reverse proxies must normalise trusted forwarded headers before the request
    reaches the app, for example with Uvicorn's `--proxy-headers` and a scoped
    `--forwarded-allow-ips` value. This function deliberately does not trust raw
    forwarding headers itself. Set ``force_secure`` only when the deployment
    cannot provide a reliable ASGI request scheme but still terminates TLS
    before the browser.

    Only HTTP/HTTPS schemes are interpreted directly. Alternate secure schemes
    can be mapped to their canonical HTTP(S) equivalents through
    ``SECURE_SCHEME_ALIASES``.
    """

    if force_secure:
        return True

    scheme = request.scope.get("scheme")
    if not isinstance(scheme, str):
        return False

    normalised_scheme = SECURE_SCHEME_ALIASES.get(scheme, scheme)
    if normalised_scheme == "http" and _has_secure_forwarded_proto(request):
        _log_forward_header_misconfig()
    return normalised_scheme in SECURE_COOKIE_SCHEMES


def _log_forward_header_misconfig() -> None:
    global _logged_forward_header_misconfig

    message = (
        "Detected HTTPS forwarding headers while ASGI request scheme is "
        "'http'; session cookies will not be marked Secure. Configure "
        "trusted proxy headers or set session_cookie_force_secure."
    )
    if _logged_forward_header_misconfig:
        logger.debug(message)
        return

    logger.warning(message)
    _logged_forward_header_misconfig = True


def _has_secure_forwarded_proto(request: Request) -> bool:
    """Return whether advisory forwarding headers claim external HTTPS.

    This supports comma-separated ``X-Forwarded-Proto`` values and RFC 7239
    ``Forwarded`` parameters such as ``proto=https``. The result is used only to
    warn about proxy misconfiguration; cookie security still depends on the ASGI
    request scheme or an explicit force-secure setting.
    """

    forwarded_proto = request.headers.get(FORWARDED_PROTO_HEADER, "")
    if any(value.strip().lower() == "https" for value in forwarded_proto.split(",")):
        return True

    forwarded = request.headers.get(FORWARDED_HEADER, "")
    for entry in forwarded.split(","):
        for parameter in entry.split(";"):
            key, separator, value = parameter.partition("=")
            if separator != "=" or key.strip().lower() != "proto":
                continue

            if value.strip().strip('"').strip("'").lower() == "https":
                return True

    return False


def _cached_current_user(request: Request, token: str | None) -> User | None | object:
    cached_token = getattr(request.state, _CURRENT_USER_CACHE_TOKEN_ATTR, None)
    if cached_token != token:
        return _CURRENT_USER_CACHE_MISSING

    return getattr(
        request.state,
        _CURRENT_USER_CACHE_VALUE_ATTR,
        _CURRENT_USER_CACHE_MISSING,
    )


def _cache_current_user(
    request: Request,
    token: str | None,
    user: User | None,
) -> User | None:
    setattr(request.state, _CURRENT_USER_CACHE_TOKEN_ATTR, token)
    setattr(request.state, _CURRENT_USER_CACHE_VALUE_ATTR, user)
    return user


async def resolve_current_user(request: Request) -> User | None:
    options = _identity_options_from_request(request)
    token = request.cookies.get(options.session_cookie_name)
    cached_user = _cached_current_user(request, token)
    if cached_user is None or isinstance(cached_user, User):
        return cached_user

    if token is None:
        return _cache_current_user(request, token, None)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        strategy = create_database_strategy(session, options)
        now = current_timestamp()
        user = await strategy.read_token(token, manager)
        if user is not None and not is_user_effectively_active(user, now=now):
            await delete_session_token_by_value(session, token)
            return _cache_current_user(request, token, None)

        return _cache_current_user(request, token, user)


async def optional_current_user(request: Request) -> User | None:
    return await resolve_current_user(request)


async def require_current_user(request: Request) -> User:
    user = await resolve_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    return user


async def require_anonymous_user(request: Request) -> None:
    user = await resolve_current_user(request)
    if user is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Already authenticated.",
        )


async def authenticate_user(
    request: Request,
    email: str,
    password: str,
) -> User | None:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)
    credentials = OAuth2PasswordRequestForm(username=email, password=password)

    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        now = current_timestamp()
        user = await manager.authenticate(credentials)
        if user is None or not is_user_effectively_active(user, now=now):
            return None

        return user


async def create_local_user_from_signup(
    request: Request,
    email: str,
    password: str,
) -> Result[dict[str, Any]]:
    options = _identity_options_from_request(request)
    if options.account_creation_policy != "public-signup":
        return Result.failure(ERROR_POLICY_DISABLED)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        try:
            user_create = UserCreate(
                email=email,
                password=password,
            )
        except ValidationError:
            return Result.failure(ERROR_INVALID_EMAIL)

        try:
            user = await manager.create(
                user_create,
                safe=True,
                request=request,
            )
        except InvalidPasswordException as exc:
            return Result.failure(public_password_error_type(exc))
        except UserAlreadyExists:
            return Result.failure(ERROR_ALREADY_EXISTS)

        return Result.ok({"id": str(user.id), "email": user.email})


async def complete_authentication_ceremony(
    request: Request,
    user: User,
) -> Result[str]:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)

    async with session_factory() as session:
        current_user = await session.get(User, user.id)
        now = current_timestamp()
        if current_user is None or not is_user_effectively_active(
            current_user,
            now=now,
        ):
            return Result.failure(ERROR_INACTIVE_USER)

        current_user.last_login_at = now
        strategy = create_database_strategy(session, options)
        return Result.ok(await strategy.write_token(current_user))


async def destroy_session_token(request: Request) -> None:
    options = _identity_options_from_request(request)
    token = request.cookies.get(options.session_cookie_name)
    if token is None:
        return

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        await delete_session_token_by_value(session, token)

    _cache_current_user(request, token, None)


async def request_password_reset(request: Request, email: str) -> None:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)

    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        try:
            user = await manager.get_by_email(email)
        except UserNotExists:
            return

        now = current_timestamp()
        if not is_user_effectively_active(user, now=now):
            return

        try:
            await manager.forgot_password(user, request)
        except FastAPIUsersException:
            logger.warning(
                "Password reset request was rejected by the identity backend.",
                exc_info=True,
            )
            return


async def reset_password(request: Request, token: str, password: str) -> bool:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)

    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        if not await _reset_token_user_is_effectively_active(manager, token):
            return False

        try:
            await manager.reset_password(token, password, request)
        except FastAPIUsersException:
            return False

        return True


async def request_verification(request: Request, email: str) -> None:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)

    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        try:
            user = await manager.get_by_email(email)
        except UserNotExists:
            return

        now = current_timestamp()
        if not is_user_effectively_active(user, now=now):
            return

        if user.is_verified:
            return

        user.email_verification_sent_at = now
        await session.commit()

        try:
            await manager.request_verify(user, request)
        except FastAPIUsersException:
            logger.warning(
                "Verification request was rejected by the identity backend.",
                exc_info=True,
            )
            return


async def _active_user_from_verification_token(
    manager: UserManager,
    token: str,
) -> Result[str]:
    try:
        data = decode_jwt(
            token,
            manager.verification_token_secret,
            [manager.verification_token_audience],
        )
        user_id = data["sub"]
        email = data["email"]
        parsed_id = manager.parse_id(user_id)
        user = await manager.get(parsed_id)
    except KeyError:
        return Result.failure(ERROR_INVALID_TOKEN)
    except PyJWTError:
        return Result.failure(ERROR_INVALID_TOKEN)
    except InvalidID:
        return Result.failure(ERROR_INVALID_TOKEN)
    except UserNotExists:
        return Result.failure(ERROR_INVALID_TOKEN)

    if email != user.email:
        return Result.failure(ERROR_IDENTITY_CHANGED)

    now = current_timestamp()
    if not is_user_effectively_active(user, now=now):
        return Result.failure(ERROR_INACTIVE_USER)

    return Result.ok(str(user.id))


async def _reset_token_user_is_effectively_active(
    manager: UserManager,
    token: str,
) -> bool:
    try:
        data = decode_jwt(
            token,
            manager.reset_password_token_secret,
            [manager.reset_password_token_audience],
        )
        user_id = data["sub"]
        parsed_id = manager.parse_id(user_id)
        user = await manager.get(parsed_id)
    except KeyError:
        return False
    except PyJWTError:
        return False
    except InvalidID:
        return False
    except UserNotExists:
        return False

    now = current_timestamp()
    return is_user_effectively_active(user, now=now)


async def verify_user(request: Request, token: str) -> Result[str]:
    options = _identity_options_from_request(request)
    session_factory = _session_factory_from_request(request)

    async with session_factory() as session:
        manager = create_user_manager(
            session,
            options,
            _delivery_from_request(request),
        )
        token_result = await _active_user_from_verification_token(manager, token)
        if token_result.is_failure():
            return token_result

        try:
            verified_user = await manager.verify(token, request)
        except InvalidVerifyToken:
            return Result.failure(ERROR_INVALID_TOKEN)
        except UserAlreadyVerified:
            return Result.failure(ERROR_ALREADY_VERIFIED)
        except FastAPIUsersException as exc:
            logger.warning(
                "Verification token was rejected by the identity backend: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return Result.failure(ERROR_TOKEN_REJECTED)

        return Result.ok(str(verified_user.id))
