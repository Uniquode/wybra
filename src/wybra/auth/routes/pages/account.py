from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth.capabilities import login_required
from wybra.auth.models import User
from wybra.auth.options import TOTP_DISABLED, TOTP_REQUIRED
from wybra.auth.provider_support import (
    enabled_google_provider as _enabled_google_provider,
)
from wybra.auth.provider_support import (
    google_link_path as _google_link_path,
)
from wybra.auth.provider_support import (
    local_password_login_usable as _local_password_login_usable,
)
from wybra.auth.provider_support import (
    provider_credential_store_from_request as _provider_credential_store,
)
from wybra.auth.provider_support import (
    user_has_usable_account_sign_in as _user_has_usable_account_sign_in,
)
from wybra.auth.routes.paths import (
    optional_route_path as _optional_route_path,
)
from wybra.auth.routes.paths import (
    route_path as _route_path,
)
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
from wybra.forms import request_form_data
from wybra.providers.google import GOOGLE_PROVIDER_NAME
from wybra.template import render_page

from .shared import (
    _form_value,
    _identity_context,
    _identity_options,
    _load_active_totp_credential_id,
    _load_user_by_id,
    _public_signup_enabled,
    _session_factory_from_request,
    account_router,
)

LOGIN_REQUIRED = Depends(login_required)


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

    return render_page(request, "identity/pages/account.html", context)


@account_router.get(
    "/security",
    include_in_schema=False,
    name="auth:security",
)
async def security(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    return await _security_page_response(request, user)


@account_router.post(
    "/security/providers/google/unlink",
    include_in_schema=False,
    name="auth:security-google-unlink",
)
async def unlink_google_provider(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    form_data = await request_form_data(request)
    provider_id = _form_value(form_data, "provider_id")
    session_factory = _session_factory_from_request(request)
    error: str | None = None
    async with session_factory() as session:
        async with session.begin():
            db_user = await _load_user_by_id(session, user.id)
            if db_user is None:
                raise HTTPException(status_code=401, detail="Authentication required.")

            store = _provider_credential_store(request, session)
            provider = await store.get_user_provider_by_id(
                user_id=db_user.id,
                provider_id=provider_id,
            )
            if provider is None or provider.provider_name != GOOGLE_PROVIDER_NAME:
                return RedirectResponse(
                    url=_route_path(request, "auth:security"),
                    status_code=303,
                )

            if not await _user_has_usable_account_sign_in(
                request,
                session,
                db_user,
                exclude_provider_id=provider.id,
            ):
                error = "Add another sign-in method before unlinking Google."
            else:
                await store.unlink_user_provider(
                    user_id=db_user.id,
                    provider_id=provider.id,
                )

    if error is not None:
        return await _security_page_response(
            request,
            user,
            form_error=error,
            status_code=400,
        )
    return RedirectResponse(url=_route_path(request, "auth:security"), status_code=303)


@account_router.post(
    "/security/password/disable",
    include_in_schema=False,
    name="auth:security-password-disable",
)
async def disable_password_login(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    session_factory = _session_factory_from_request(request)
    error: str | None = None
    async with session_factory() as session:
        db_user = await _load_user_by_id(session, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")

        if not _local_password_login_usable(db_user):
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if not await _user_has_usable_account_sign_in(
            request,
            session,
            db_user,
            exclude_password=True,
        ):
            error = "Add another sign-in method before disabling password sign-in."
        else:
            db_user.password_login_enabled = False
            await session.commit()

    if error is not None:
        return await _security_page_response(
            request,
            user,
            form_error=error,
            status_code=400,
        )
    return RedirectResponse(url=_route_path(request, "auth:security"), status_code=303)


async def _security_page_response(
    request: Request,
    user: User,
    *,
    form_error: str | None = None,
    form_message: str | None = None,
    status_code: int = 200,
) -> Response:
    context = _identity_context(
        request,
        page_title="Login & Security",
        user=user,
        form_error=form_error,
        form_message=form_message,
        password_login=await _security_password_section(request, user),
        providers=await _security_provider_section(request, user),
        totp=await _security_totp_section(request, user),
    )
    return render_page(
        request,
        "identity/pages/security.html",
        context,
        status_code=status_code,
    )


async def _security_totp_section(request: Request, user: User) -> dict[str, object]:
    options = _identity_options(request)
    if options.totp_mode == TOTP_DISABLED:
        return {"available": False}

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        active_credential_id = await _load_active_totp_credential_id(session, user.id)

    return {
        "available": True,
        "enabled": active_credential_id is not None,
        "setup_path": _totp_setup_path(request),
    }


def _totp_setup_path(request: Request) -> str:
    security_path = _route_path(request, "auth:security")
    setup_query = urlencode({"return_to": security_path})
    return f"{_route_path(request, 'auth:totp-setup')}?{setup_query}"


async def _security_password_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    enabled = _local_password_login_usable(user)
    disable_path = _optional_route_path(request, "auth:security-password-disable")
    disable_available = False
    if (
        enabled
        and disable_path is not None
        and _enabled_google_provider(request) is not None
    ):
        session_factory = _session_factory_from_request(request)
        async with session_factory() as session:
            db_user = await _load_user_by_id(session, user.id)
            disable_available = (
                db_user is not None
                and await _user_has_usable_account_sign_in(
                    request,
                    session,
                    db_user,
                    exclude_password=True,
                )
            )

    return {
        "available": True,
        "enabled": enabled,
        "disable_available": disable_available,
        "disable_path": disable_path,
    }


async def _security_provider_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    google_section = await _security_google_provider_section(request, user)
    providers = (google_section,) if google_section["available"] else ()
    return {
        "available": bool(providers),
        "providers": providers,
    }


async def _security_google_provider_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    link_path = _google_link_path(
        request,
        return_to=_optional_route_path(request, "auth:security"),
    )
    unlink_path = _optional_route_path(request, "auth:security-google-unlink")
    if (
        _enabled_google_provider(request) is None
        or link_path is None
        or unlink_path is None
    ):
        return {"available": False}

    linked_accounts: list[dict[str, str]] = []
    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        store = _provider_credential_store(request, session)
        providers = await store.get_user_providers(
            user_id=user.id,
            provider_name=GOOGLE_PROVIDER_NAME,
        )
        for provider in providers:
            if not provider.provider_enabled:
                continue
            linked_accounts.append(
                {
                    "id": str(provider.id),
                    "account_email": provider.account_email,
                }
            )

    return {
        "available": True,
        "name": GOOGLE_PROVIDER_NAME,
        "label": "Google",
        "linked": bool(linked_accounts),
        "linked_accounts": tuple(linked_accounts),
        "link_path": link_path,
        "unlink_path": unlink_path,
    }


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
