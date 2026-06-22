import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.storage import (
    SqlAlchemyChallengeStore,
    SqlAlchemyTOTPCredentialStore,
)
from wybra.auth.models import User
from wybra.auth.options import IdentityOptions
from wybra.auth.routes.totp import (
    TOTP_LOGIN_CHALLENGE_ERROR_BY_MESSAGE,
    TOTP_LOGIN_FORM_ERROR_BY_MESSAGE,
    TOTP_LOGIN_NONCE_FIELD,
    TOTP_SETUP_CHALLENGE_FLAG,
    TOTP_SETUP_NONCE_FIELD,
    generate_totp_login_nonce,
    generate_totp_setup_nonce,
    login_context,
)
from wybra.auth.sessions import (
    authenticate_user,
    complete_authentication_ceremony,
    resolve_current_user,
    set_session_cookie,
)
from wybra.auth.settings import identity_options_from_state
from wybra.auth.timestamps import current_timestamp
from wybra.core.routes.contracts import API_PATH_PREFIX
from wybra.db import DatabaseCapability
from wybra.forms import request_form_data, validate_csrf
from wybra.site import get_site
from wybra.template import render_page

account_router = APIRouter(dependencies=[Depends(validate_csrf)])
api_router = APIRouter(prefix=f"{API_PATH_PREFIX.rstrip('/')}/identity")
logger = logging.getLogger(__name__)


def _identity_context(request: Request, **extra: Any) -> dict[str, Any]:
    del request
    return dict(extra)


def _form_value(form_data: FormData, name: str, default: str = "") -> str:
    value = form_data.get(name, default)
    return value if isinstance(value, str) else default


def _identity_options(request: Request) -> IdentityOptions:
    return identity_options_from_state(request.app.state)


def _public_signup_enabled(request: Request) -> bool:
    return _identity_options(request).account_creation_policy == "public-signup"


def normalise_return_to(value: str | None, default: str = "/account") -> str:
    candidate = (value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        return default

    decoded_candidate = unquote(candidate)
    if (
        decoded_candidate.startswith("//")
        or "\\" in decoded_candidate
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in decoded_candidate
        )
    ):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default

    return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _session_factory_from_request(
    request: Request,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    return get_site(request.app).require_capability(DatabaseCapability).session


async def _load_user_by_id(
    session: AsyncSession,
    user_id: str | UUID,
) -> User | None:
    parsed_user_id = parse_uuid(user_id)
    if parsed_user_id is None:
        return None

    return await session.get(User, parsed_user_id)


async def _load_active_totp_credential_id(
    session: AsyncSession,
    user_id: str | UUID,
) -> str | None:
    active_credential_id, _pending_credential_id = await _load_totp_credential_ids(
        session,
        user_id,
    )
    return active_credential_id


async def _load_totp_credential_ids(
    session: AsyncSession,
    user_id: str | UUID,
) -> tuple[str | None, str | None]:
    parsed_user_id = parse_uuid(user_id)
    if parsed_user_id is None:
        return None, None

    parsed_user_id_text = str(parsed_user_id)
    store = SqlAlchemyTOTPCredentialStore(session)
    return (
        await store.get_active_totp_credential(parsed_user_id_text),
        await store.get_pending_totp_credential(parsed_user_id_text),
    )


async def _require_authenticated_user(request: Request) -> User:
    user = await resolve_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    return user


async def _fresh_primary_assertion_satisfied(
    request: Request,
    user: User,
) -> bool:
    form_data = await request_form_data(request)
    password = _form_value(form_data, "password")
    if not password:
        return False

    asserted_user = await authenticate_user(request, user.email, password)
    return bool(asserted_user is not None and asserted_user.id == user.id)


async def _create_totp_login_challenge(
    request: Request,
    user_id: str,
    credential_id: str,
) -> tuple[str, str]:
    options = _identity_options(request)
    session_factory = _session_factory_from_request(request)
    now = current_timestamp()
    challenge_expires_at = now + options.totp_challenge_expiry_seconds
    login_nonce = generate_totp_login_nonce()
    async with session_factory() as session:
        challenge_store = SqlAlchemyChallengeStore(session)
        challenge = await challenge_store.create_challenge(
            user_id=user_id,
            kind="totp",
            expires_at=challenge_expires_at,
            metadata={
                TOTP_LOGIN_NONCE_FIELD: login_nonce,
                "totp_credential_id": credential_id,
            },
        )
        await session.commit()
        return challenge.id, login_nonce


async def _create_totp_setup_challenge(
    request: Request,
    user_id: str,
    return_to: str,
) -> tuple[str, str]:
    options = _identity_options(request)
    session_factory = _session_factory_from_request(request)
    now = current_timestamp()
    challenge_expires_at = now + options.totp_challenge_expiry_seconds
    setup_nonce = generate_totp_setup_nonce()
    async with session_factory() as session:
        challenge_store = SqlAlchemyChallengeStore(session)
        challenge = await challenge_store.create_challenge(
            user_id=user_id,
            kind="totp",
            expires_at=challenge_expires_at,
            metadata={
                TOTP_SETUP_CHALLENGE_FLAG: True,
                TOTP_SETUP_NONCE_FIELD: setup_nonce,
                "return_to": return_to,
            },
        )
        await session.commit()
        return challenge.id, setup_nonce


def _totp_setup_return_to(metadata: object, *, default: str = "/account") -> str:
    if not isinstance(metadata, Mapping):
        return default

    return_to = cast(Mapping[str, object], metadata).get("return_to")
    if not isinstance(return_to, str):
        return default

    return normalise_return_to(return_to, default=default)


def _is_effectively_active_user(user: User, *, now: float | None = None) -> bool:
    if not user.is_active:
        return False

    expires_at = user.expires_at
    if expires_at is None:
        return True

    return expires_at > (current_timestamp() if now is None else now)


def _totp_login_error_response(
    request: Request,
    *,
    email: str,
    return_to: str,
    message: str,
    status_code: int = 401,
    challenge_id: str | None = None,
    challenge_step: str = "totp",
    challenge_error: str | None = None,
) -> Response:
    return _login_error_response(
        request,
        email=email,
        return_to=return_to,
        status_code=status_code,
        message=message,
        challenge_id=challenge_id,
        challenge_error=challenge_error,
        requires_email_password=False,
        challenge_step=challenge_step,
    )


async def _complete_login_ceremony(
    request: Request,
    user: User,
    *,
    return_to: str,
) -> Response:
    ceremony_result = await complete_authentication_ceremony(request, user)
    if ceremony_result.is_failure() or ceremony_result.value is None:
        return _login_error_response(
            request,
            email=user.email,
            return_to=return_to,
            status_code=401,
        )

    response = RedirectResponse(url=return_to, status_code=303)
    set_session_cookie(
        response, request, ceremony_result.value, _identity_options(request)
    )
    return response


def _login_error_response(
    request: Request,
    *,
    email: str,
    return_to: str,
    status_code: int = 401,
    message: str | None = None,
    challenge_id: str | None = None,
    challenge_step: str | None = None,
    challenge_error: str | None = None,
    requires_email_password: bool = True,
) -> Response:
    form_error = (
        TOTP_LOGIN_FORM_ERROR_BY_MESSAGE[message]
        if message in TOTP_LOGIN_FORM_ERROR_BY_MESSAGE
        else "Email or password is incorrect."
    )
    if challenge_error is None and message in TOTP_LOGIN_CHALLENGE_ERROR_BY_MESSAGE:
        challenge_error = TOTP_LOGIN_CHALLENGE_ERROR_BY_MESSAGE[message]

    context = login_context(
        request,
        email=email,
        return_to=return_to,
        form_error=form_error if message else "Email or password is incorrect.",
        challenge_id=challenge_id,
        challenge_step=challenge_step,
        challenge_error=challenge_error,
        requires_email_password=requires_email_password,
    )
    return render_page(
        request,
        "identity/pages/login.html",
        context,
        status_code=status_code,
    )


__all__ = [name for name in globals() if not name.startswith("__")]
