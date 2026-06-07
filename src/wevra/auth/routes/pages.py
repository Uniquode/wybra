from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from wevra.auth.options import IdentityOptions
from wevra.auth.sessions import (
    authenticate_user,
    clear_session_cookie,
    complete_authentication_ceremony,
    create_local_user_from_signup,
    destroy_session_token,
    request_password_reset,
    request_verification,
    reset_password,
    resolve_current_user,
    set_session_cookie,
    verify_user,
)
from wevra.web.forms.csrf import request_form_data, validate_csrf
from wevra.web.rendering import render_page
from wevra.web.routes.contracts import API_PATH_PREFIX

account_router = APIRouter(dependencies=[Depends(validate_csrf)])
api_router = APIRouter(prefix=f"{API_PATH_PREFIX.rstrip('/')}/identity")


async def current_user_state(request: Request) -> dict[str, object]:
    user = await resolve_current_user(request)
    if user is None:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": user.email,
        # Keep this optional state endpoint out of authorisation decisions.
        "is_verified": user.is_verified,
    }


def _identity_context(request: Request, **extra: Any) -> dict[str, Any]:
    del request
    return dict(extra)


def _form_value(form_data: FormData, name: str, default: str = "") -> str:
    value = form_data.get(name, default)
    return value if isinstance(value, str) else default


def _identity_options(request: Request) -> IdentityOptions:
    options = getattr(request.app.state, "identity_options", None)
    if not isinstance(options, IdentityOptions):
        raise RuntimeError("Identity options are not configured on the application.")

    return options


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


@api_router.get(
    "/current-user",
    include_in_schema=False,
    name="auth:api:current-user",
)
async def current_user_api(request: Request) -> dict[str, object]:
    return await current_user_state(request)


@account_router.api_route(
    "/login",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:login",
)
async def login(request: Request) -> Response:
    if request.method == "GET":
        context = _identity_context(
            request,
            page_title="Sign in",
            public_signup_enabled=_public_signup_enabled(request),
            return_to=normalise_return_to(request.query_params.get("return_to")),
        )
        return render_page(request, "identity/pages/login.html", context)

    form_data = await request_form_data(request)
    email = _form_value(form_data, "email").strip()
    password = _form_value(form_data, "password")
    return_to = normalise_return_to(_form_value(form_data, "return_to"))

    user = await authenticate_user(request, email, password)
    if user is None:
        return _login_error_response(request, email=email, return_to=return_to)

    ceremony_result = await complete_authentication_ceremony(request, user)
    if ceremony_result.is_failure() or ceremony_result.value is None:
        return _login_error_response(request, email=email, return_to=return_to)

    response = RedirectResponse(url=return_to, status_code=303)
    set_session_cookie(
        response,
        request,
        ceremony_result.value,
        _identity_options(request),
    )
    return response


def _login_error_response(
    request: Request,
    *,
    email: str,
    return_to: str,
) -> Response:
    context = _identity_context(
        request,
        page_title="Sign in",
        public_signup_enabled=_public_signup_enabled(request),
        email=email,
        return_to=return_to,
        form_error="Email or password is incorrect.",
    )
    return render_page(
        request,
        "identity/pages/login.html",
        context,
        status_code=401,
    )


@account_router.api_route(
    "/signup",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:signup",
)
async def signup(request: Request) -> Response:
    if not _public_signup_enabled(request):
        raise HTTPException(status_code=404)

    if request.method == "GET":
        context = _identity_context(request, page_title="Create account")
        return render_page(request, "identity/pages/signup.html", context)

    form_data = await request_form_data(request)
    email = _form_value(form_data, "email").strip()
    password = _form_value(form_data, "password")
    creation_result = await create_local_user_from_signup(
        request,
        email,
        password,
    )
    created = creation_result.is_ok()
    context = _identity_context(
        request,
        page_title="Create account",
        email=email,
        form_message=("Account created. You can now sign in." if created else None),
        form_error=(
            None if created else "Unable to create account with those details."
        ),
    )
    return render_page(
        request,
        "identity/pages/signup.html",
        context,
        status_code=201 if created else 400,
    )


@account_router.api_route(
    "/logout",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:logout",
)
async def logout(request: Request) -> Response:
    if request.method == "GET":
        context = _identity_context(request, page_title="Sign out")
        return render_page(request, "identity/pages/logout.html", context)

    await destroy_session_token(request)
    response = RedirectResponse(url="/", status_code=303)
    clear_session_cookie(response, request, _identity_options(request))
    return response


@account_router.get("", include_in_schema=False, name="auth:account")
async def account(request: Request) -> Response:
    context = _identity_context(
        request,
        page_title="Account",
    )
    return render_page(request, "identity/pages/account.html", context)


@account_router.api_route(
    "/password/reset",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:password-reset",
)
async def password_reset(request: Request) -> Response:
    if request.method == "GET":
        context = _identity_context(request, page_title="Reset password")
        return render_page(request, "identity/pages/password_reset.html", context)

    form_data = await request_form_data(request)
    email = _form_value(form_data, "email").strip()
    await request_password_reset(request, email)
    context = _identity_context(
        request,
        page_title="Reset password",
        email=email,
        form_message="If the account exists, a reset link has been queued.",
    )
    return render_page(request, "identity/pages/password_reset.html", context)


@account_router.post(
    "/password/reset/confirm",
    include_in_schema=False,
    name="auth:password-reset-confirm",
)
async def password_reset_confirm(request: Request) -> Response:
    form_data = await request_form_data(request)
    token = _form_value(form_data, "token")
    password = _form_value(form_data, "password")
    did_reset = await reset_password(request, token, password)
    context = _identity_context(
        request,
        page_title="Reset password",
        form_message="Password reset complete." if did_reset else None,
        form_error=None if did_reset else "The reset token is invalid or expired.",
    )
    return render_page(
        request,
        "identity/pages/password_reset.html",
        context,
        status_code=200 if did_reset else 400,
    )


@account_router.api_route(
    "/verify",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:verify",
)
async def verify(request: Request) -> Response:
    if request.method == "GET":
        context = _identity_context(request, page_title="Verify email")
        return render_page(request, "identity/pages/verify.html", context)

    form_data = await request_form_data(request)
    email = _form_value(form_data, "email").strip()
    await request_verification(request, email)
    context = _identity_context(
        request,
        page_title="Verify email",
        email=email,
        form_message=(
            "If the account can be verified, a verification link has been queued."
        ),
    )
    return render_page(request, "identity/pages/verify.html", context)


@account_router.post(
    "/verify/confirm",
    include_in_schema=False,
    name="auth:verify-confirm",
)
async def verify_confirm(request: Request) -> Response:
    form_data = await request_form_data(request)
    token = _form_value(form_data, "token")
    verification_result = await verify_user(request, token)
    did_verify = verification_result.is_ok()
    context = _identity_context(
        request,
        page_title="Verify email",
        form_message="Email verification complete." if did_verify else None,
        form_error=(
            None if did_verify else "The verification token is invalid or expired."
        ),
    )
    return render_page(
        request,
        "identity/pages/verify.html",
        context,
        status_code=200 if did_verify else 400,
    )


module_routers = {
    "account": account_router,
    "api": api_router,
}

__all__ = [
    "account",
    "account_router",
    "api_router",
    "current_user_api",
    "current_user_state",
    "login",
    "logout",
    "module_routers",
    "normalise_return_to",
    "password_reset",
    "password_reset_confirm",
    "signup",
    "verify",
    "verify_confirm",
]
