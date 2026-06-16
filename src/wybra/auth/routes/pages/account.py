from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth.options import TOTP_DISABLED, TOTP_REQUIRED
from wybra.auth.sessions import (
    clear_session_cookie,
    create_local_user_from_signup,
    destroy_session_token,
    request_password_reset,
    request_verification,
    reset_password,
    resolve_current_user,
    verify_user,
)
from wybra.web.forms.csrf import request_form_data
from wybra.web.rendering import render_page

from .shared import (
    _form_value,
    _identity_context,
    _identity_options,
    _load_active_totp_credential_id,
    _load_totp_credential_ids,
    _public_signup_enabled,
    _session_factory_from_request,
    account_router,
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
    user = await resolve_current_user(request)
    if user is None:
        return render_page(request, "identity/pages/account.html", context)

    options = _identity_options(request)
    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        active_totp_id, pending_totp_id = await _load_totp_credential_ids(
            session,
            str(user.id),
        )

    context |= {
        "totp_mode": options.totp_mode,
        "totp_enabled": options.totp_mode != TOTP_DISABLED,
        "totp_has_active_credential": active_totp_id is not None,
        "totp_has_pending_credential": pending_totp_id is not None,
        "totp_setup_path": request.url_for("auth:totp-setup"),
        "totp_disable_path": request.url_for("auth:totp-disable"),
        "totp_reset_path": request.url_for("auth:totp-reset"),
    }
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
    options = _identity_options(request)
    totp_setup_required = False
    if did_verify and options.totp_mode == TOTP_REQUIRED and verification_result.value:
        session_factory = _session_factory_from_request(request)
        async with session_factory() as session:
            totp_setup_required = (
                await _load_active_totp_credential_id(
                    session, verification_result.value
                )
            ) is None

    context = _identity_context(
        request,
        page_title="Verify email",
        form_message="Email verification complete." if did_verify else None,
        form_error=(
            None if did_verify else "The verification token is invalid or expired."
        ),
        totp_setup_required=totp_setup_required,
        totp_login_path=request.url_for("auth:login"),
    )
    return render_page(
        request,
        "identity/pages/verify.html",
        context,
        status_code=200 if did_verify else 400,
    )
