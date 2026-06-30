"""TOTP route helpers shared by account page handlers."""

from __future__ import annotations

import logging
from secrets import token_urlsafe
from typing import Any, Final
from urllib.parse import quote, urlencode

import qrcode
import qrcode.image.svg
from fastapi import HTTPException, Request
from fastapi.responses import Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.challenges import (
    TOTP_ASSERTION_METHOD,
    AuthenticationAssertion,
    AuthenticationMethod,
    required_authentication_methods_for_totp_policy,
)
from wybra.auth.mfa.storage import (
    TOTP_ACTIVE_STATUS,
    ChallengeRecord,
    SqlAlchemyRecoveryCodeStore,
    SqlAlchemyTOTPCredentialStore,
)
from wybra.auth.mfa.totp import DEFAULT_TOTP_DIGITS, is_valid_totp_code, verify_totp
from wybra.auth.models import IdentityTotpCredential
from wybra.auth.options import TOTP_DISABLED, IdentityOptions
from wybra.auth.result import (
    ERROR_TOTP_CODE_REQUIRED,
    ERROR_TOTP_INVALID,
    ERROR_TOTP_RECOVERY_INVALID,
    ERROR_TOTP_SETUP_REQUIRED,
    ERROR_VERIFICATION_CODE_INVALID,
)
from wybra.auth.sessions import session_cookie_secure_for_request
from wybra.auth.settings import auth_settings_from_state, identity_options_from_state
from wybra.auth.timestamps import current_timestamp
from wybra.services.crypto import SecretDataError, SecretMaterialMissingError
from wybra.template import render_page

logger = logging.getLogger(__name__)
TOTP_SETUP_BYPASS_TOKEN: Final[str] = "bypass_totp_setup"
TOTP_SETUP_CHALLENGE_FLAG: Final[str] = "totp_setup"
TOTP_LOGIN_NONCE_COOKIE: Final[str] = "wybra_totp_login"
TOTP_LOGIN_NONCE_FIELD: Final[str] = "login_nonce"
TOTP_SETUP_NONCE_COOKIE: Final[str] = "wybra_totp_setup"
TOTP_SETUP_NONCE_BYTES: Final[int] = 32
TOTP_SETUP_NONCE_FIELD: Final[str] = "setup_nonce"
TOTP_LOGIN_FORM_ERROR_BY_MESSAGE: Final[dict[str, str]] = {
    ERROR_TOTP_CODE_REQUIRED: "Two-factor verification failed.",
    ERROR_TOTP_INVALID: "Two-factor verification failed.",
    ERROR_TOTP_RECOVERY_INVALID: "Two-factor verification failed.",
    ERROR_TOTP_SETUP_REQUIRED: "Complete authenticator setup to continue.",
    ERROR_VERIFICATION_CODE_INVALID: "Two-factor verification failed.",
}
TOTP_LOGIN_CHALLENGE_ERROR_BY_MESSAGE: Final[dict[str, str]] = {
    ERROR_TOTP_CODE_REQUIRED: (
        "Enter the code from your authenticator app or a one-time use recovery code."
    ),
    ERROR_TOTP_INVALID: "Verification code is invalid.",
    ERROR_TOTP_RECOVERY_INVALID: "Verification code is invalid.",
    ERROR_TOTP_SETUP_REQUIRED: (
        "Set up an authenticator before continuing, or choose to bypass this step."
    ),
    ERROR_VERIFICATION_CODE_INVALID: "Verification code is invalid.",
}
TOTP_CODE_REPLAY_MESSAGE: Final[str] = "Authenticator code has already been used."
RECOVERY_CODES_DOWNLOAD_FILENAME: Final[str] = "recovery-codes.txt"
TOTP_SETUP_PAGE_MESSAGES: Final[dict[str, str]] = {
    "complete": "Your authenticator has been enabled.",
    "store_recovery_codes": "Store these recovery codes in a secure location:",
    "return_to_account": "Return to account",
    "return_to_security": "Return to Login & Security",
    "download_recovery_codes": f"Download {RECOVERY_CODES_DOWNLOAD_FILENAME}",
    "instructions": (
        "Use your authenticator app to add this account, then enter the first code."
    ),
    "show_setup_qr_code": "Show setup QRCode",
    "show_setup_uri": "Show setup URI",
    "setup_uri": "Setup URI:",
    "show_secret": "Show secret",
    "secret": "Secret:",
    "verification_code": "Authenticator code",
    "confirm_setup": "Confirm setup",
    "verify_email": "Verify your email before setting up an authenticator.",
    "initialise_error": "Unable to initialise authenticator setup.",
    "code_required": "Enter a verification code.",
    "invalid_code": "Invalid authenticator code.",
    "code_replay": TOTP_CODE_REPLAY_MESSAGE,
}


def _identity_options(request: Request) -> IdentityOptions:
    return identity_options_from_state(request.app.state)


def _identity_context(request: Request, **extra: Any) -> dict[str, Any]:
    del request
    return dict(extra)


def _public_signup_enabled(request: Request) -> bool:
    return _identity_options(request).account_creation_policy == "public-signup"


def totp_required_methods(
    options: IdentityOptions,
    *,
    has_active_totp: bool,
) -> tuple[AuthenticationMethod, ...]:
    return required_authentication_methods_for_totp_policy(
        totp_enabled=options.totp_mode != TOTP_DISABLED,
        has_active_totp=has_active_totp,
    )


def totp_assertion(
    user_id: str,
    *,
    ceremony_id: str,
    asserted_at: float | None = None,
) -> AuthenticationAssertion:
    return AuthenticationAssertion(
        user_id=user_id,
        method=TOTP_ASSERTION_METHOD,
        asserted_at=current_timestamp() if asserted_at is None else asserted_at,
        ceremony_id=ceremony_id,
    )


def ensure_totp_setup_supported(request: Request) -> None:
    settings = auth_settings_from_state(request.app.state)
    if not settings.is_totp_enabled():
        raise HTTPException(status_code=404)


def totp_issuer(request: Request) -> str:
    return request.url.netloc.split(":", 1)[0] or "uniquode"


def totp_credential_store(
    request: Request,
    session: AsyncSession,
) -> SqlAlchemyTOTPCredentialStore:
    secret_service = getattr(request.app.state, "secret_envelope_service", None)
    if secret_service is None:
        return SqlAlchemyTOTPCredentialStore(session)

    return SqlAlchemyTOTPCredentialStore(session, secret_service)


def recovery_code_store(
    request: Request,
    session: AsyncSession,
) -> SqlAlchemyRecoveryCodeStore:
    secret_service = getattr(request.app.state, "secret_envelope_service", None)
    if secret_service is None:
        return SqlAlchemyRecoveryCodeStore(session)

    return SqlAlchemyRecoveryCodeStore(session, secret_service)


def login_context(
    request: Request,
    *,
    email: str = "",
    return_to: str = "/account",
    form_error: str | None = None,
    challenge_id: str | None = None,
    challenge_step: str | None = None,
    challenge_error: str | None = None,
    setup_prompt: bool = False,
    setup_challenge_id: str = "",
    totp_setup_bypass_token: str | None = None,
    setup_bypass_error: str | None = None,
    success_message: str | None = None,
    requires_email_password: bool = True,
) -> dict[str, Any]:
    setup_path_query = urlencode(
        {
            "setup_challenge_id": setup_challenge_id,
            "return_to": return_to,
        }
    )
    return _identity_context(
        request,
        page_title="Sign in",
        public_signup_enabled=_public_signup_enabled(request),
        email=email,
        return_to=return_to,
        form_error=form_error,
        challenge_id=challenge_id,
        challenge_step=challenge_step,
        challenge_error=challenge_error,
        setup_prompt=setup_prompt,
        setup_challenge_id=setup_challenge_id,
        setup_totp_path=(f"{request.url_for('auth:totp-setup')}?{setup_path_query}"),
        totp_setup_bypass_token=totp_setup_bypass_token,
        setup_bypass_error=setup_bypass_error,
        form_message=success_message,
        challenge_required=(challenge_step is not None),
        requires_email_password=requires_email_password,
        totp_code_length=DEFAULT_TOTP_DIGITS,
    )


def is_totp_setup_challenge(challenge: ChallengeRecord | None) -> bool:
    return bool(
        challenge is not None
        and challenge.kind == "totp"
        and isinstance(challenge.metadata, dict)
        and challenge.metadata.get(TOTP_SETUP_CHALLENGE_FLAG) is True
    )


def totp_setup_nonce_valid(request: Request, challenge: ChallengeRecord) -> bool:
    metadata_nonce = challenge.metadata_payload.get(TOTP_SETUP_NONCE_FIELD)
    cookie_nonce = request.cookies.get(TOTP_SETUP_NONCE_COOKIE)
    return bool(
        isinstance(metadata_nonce, str)
        and metadata_nonce
        and isinstance(cookie_nonce, str)
        and cookie_nonce == metadata_nonce
    )


def generate_totp_setup_nonce() -> str:
    return token_urlsafe(TOTP_SETUP_NONCE_BYTES)


def generate_totp_login_nonce() -> str:
    return token_urlsafe(TOTP_SETUP_NONCE_BYTES)


def totp_login_nonce_valid(request: Request, challenge: ChallengeRecord) -> bool:
    metadata_nonce = challenge.metadata_payload.get(TOTP_LOGIN_NONCE_FIELD)
    cookie_nonce = request.cookies.get(TOTP_LOGIN_NONCE_COOKIE)
    return bool(
        isinstance(metadata_nonce, str)
        and metadata_nonce
        and isinstance(cookie_nonce, str)
        and cookie_nonce == metadata_nonce
    )


def set_totp_login_nonce_cookie(
    response: Response,
    request: Request,
    nonce: str,
) -> None:
    response.set_cookie(
        TOTP_LOGIN_NONCE_COOKIE,
        nonce,
        httponly=True,
        samesite="lax",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=_identity_options(request).session_cookie_force_secure,
        ),
    )


def clear_totp_login_nonce_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        TOTP_LOGIN_NONCE_COOKIE,
        "",
        httponly=True,
        max_age=0,
        samesite="lax",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=_identity_options(request).session_cookie_force_secure,
        ),
    )


def set_totp_setup_nonce_cookie(
    response: Response,
    request: Request,
    nonce: str,
) -> None:
    response.set_cookie(
        TOTP_SETUP_NONCE_COOKIE,
        nonce,
        httponly=True,
        samesite="lax",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=_identity_options(request).session_cookie_force_secure,
        ),
    )


def clear_totp_setup_nonce_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        TOTP_SETUP_NONCE_COOKIE,
        "",
        httponly=True,
        max_age=0,
        samesite="lax",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=_identity_options(request).session_cookie_force_secure,
        ),
    )


def totp_credential_problem(
    credential: IdentityTotpCredential | None,
    *,
    expected_user_id: str,
    expected_status: str,
) -> str | None:
    if credential is None:
        return "missing_credential"
    if str(credential.user_id) != expected_user_id:
        return "user_mismatch"
    if credential.status != expected_status:
        return f"invalid_status:{credential.status}"
    return None


async def verify_totp_code_for_credential(
    *,
    session: AsyncSession,
    store: SqlAlchemyTOTPCredentialStore,
    credential_id: str,
    user_id: str,
    code: str,
    options: IdentityOptions,
    expected_status: str = TOTP_ACTIVE_STATUS,
    timestamp: float | None = None,
) -> tuple[bool, int | None, str | None]:
    parsed_credential_id = parse_uuid(credential_id)
    if parsed_credential_id is None:
        return False, None, None

    if not is_valid_totp_code(code):
        return False, None, None

    credential = (
        await session.execute(
            select(IdentityTotpCredential)
            .where(IdentityTotpCredential.id == parsed_credential_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        credential is None
        or str(credential.user_id) != user_id
        or credential.status != expected_status
    ):
        return False, None, None

    verification_time = current_timestamp() if timestamp is None else timestamp
    try:
        secret = store.decrypt_totp_secret(credential)
    except (SecretDataError, SecretMaterialMissingError) as exc:
        logger.error(
            "Unable to verify TOTP credential because secret material "
            "is unavailable or invalid: credential_id=%s user_id=%s",
            credential_id,
            user_id,
            exc_info=exc,
        )
        return False, None, None

    accepted, counter = verify_totp(
        secret,
        code,
        timestamp=verification_time,
        period=options.totp_period_seconds,
        allowed_drift=options.totp_allowed_drift,
    )
    if not accepted or counter is None:
        return False, None, None

    last_used_counter = getattr(credential, "last_used_counter", None)
    if last_used_counter is not None and counter <= last_used_counter:
        return False, None, TOTP_CODE_REPLAY_MESSAGE

    credential.last_used_counter = counter
    return True, counter, None


def render_totp_login_challenge(
    request: Request,
    *,
    return_to: str,
    email: str,
    challenge_id: str,
    error: str | None = None,
) -> Response:
    return render_page(
        request,
        "identity/pages/login.html",
        login_context(
            request,
            return_to=return_to,
            email=email,
            challenge_id=challenge_id,
            challenge_step="totp",
            requires_email_password=False,
            challenge_error=error,
        ),
        status_code=200 if error is None else 400,
    )


def render_totp_setup_prompt(
    request: Request,
    *,
    return_to: str,
    email: str,
    setup_challenge_id: str,
    setup_bypass_error: str | None = None,
) -> Response:
    return render_page(
        request,
        "identity/pages/login.html",
        login_context(
            request,
            return_to=return_to,
            email=email,
            setup_prompt=True,
            challenge_step="setup",
            setup_challenge_id=setup_challenge_id,
            totp_setup_bypass_token=TOTP_SETUP_BYPASS_TOKEN,
            setup_bypass_error=setup_bypass_error,
            challenge_error=(
                TOTP_LOGIN_CHALLENGE_ERROR_BY_MESSAGE[ERROR_TOTP_SETUP_REQUIRED]
                if setup_bypass_error is None
                else setup_bypass_error
            ),
            requires_email_password=True,
        ),
    )


def render_totp_setup_page(
    request: Request,
    *,
    return_to: str,
    setup_challenge_id: str,
    setup_totp_secret: str,
    setup_totp_uri: str,
    setup_error: str | None = None,
    setup_complete: bool = False,
    recovery_codes: tuple[str, ...] = (),
) -> Response:
    setup_return_label = (
        TOTP_SETUP_PAGE_MESSAGES["return_to_security"]
        if return_to == "/account/security"
        else TOTP_SETUP_PAGE_MESSAGES["return_to_account"]
    )
    return render_page(
        request,
        "identity/pages/totp_setup.html",
        _identity_context(
            request,
            page_title="Set up authenticator",
            return_to=return_to,
            setup_challenge_id=setup_challenge_id,
            setup_totp_secret=setup_totp_secret,
            setup_totp_uri=setup_totp_uri,
            setup_totp_qr_svg=_totp_setup_qr_svg(setup_totp_uri),
            setup_error=setup_error,
            setup_complete=setup_complete,
            setup_recovery_codes=recovery_codes,
            setup_recovery_codes_download_href=recovery_codes_download_href(
                recovery_codes
            ),
            recovery_codes_download_filename=RECOVERY_CODES_DOWNLOAD_FILENAME,
            setup_return_label=setup_return_label,
            totp_code_length=DEFAULT_TOTP_DIGITS,
            setup_messages=TOTP_SETUP_PAGE_MESSAGES,
        ),
        status_code=200,
    )


def recovery_codes_download_href(recovery_codes: tuple[str, ...]) -> str:
    if not recovery_codes:
        return ""

    recovery_codes_text = "\n".join(recovery_codes) + "\n"
    return f"data:text/plain;charset=utf-8,{quote(recovery_codes_text, safe='')}"


def _totp_setup_qr_svg(setup_totp_uri: str) -> Markup:
    if not setup_totp_uri:
        return Markup("")

    image = qrcode.make(
        setup_totp_uri,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    svg = image.to_string(encoding="unicode")
    return Markup(svg)  # nosec B704 - SVG path is generated by qrcode from URI data.


def setup_totp_error_page(
    request: Request,
    *,
    return_to: str,
    setup_challenge_id: str,
    setup_totp_secret: str = "",
    setup_totp_uri: str = "",
    setup_error: str,
) -> Response:
    return render_totp_setup_page(
        request,
        return_to=return_to,
        setup_challenge_id=setup_challenge_id or "",
        setup_totp_secret=setup_totp_secret,
        setup_totp_uri=setup_totp_uri,
        setup_error=setup_error,
        setup_complete=False,
        recovery_codes=(),
    )
