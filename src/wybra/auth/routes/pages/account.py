from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth.capabilities import login_required
from wybra.auth.forms import (
    PasskeyRevokeCommandForm,
    PasswordResetConfirmCommandForm,
    PasswordResetRequestCommandForm,
    ProviderUnlinkCommandForm,
    SignupCommandForm,
    VerificationConfirmCommandForm,
    VerificationRequestCommandForm,
    command_text,
)
from wybra.auth.mfa.webauthn import (
    passkeys_effectively_enabled as _passkeys_effectively_enabled,
)
from wybra.auth.models import User
from wybra.auth.options import TOTP_DISABLED, TOTP_REQUIRED
from wybra.auth.provider_support import (
    local_password_login_usable as _local_password_login_usable,
)
from wybra.auth.provider_support import (
    provider_credential_store_from_request as _provider_credential_store,
)
from wybra.auth.provider_support import (
    provider_label as _provider_label,
)
from wybra.auth.provider_support import (
    provider_security_options as _provider_security_options,
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
from wybra.events.auth import publish_credential_access
from wybra.forms import request_form_data
from wybra.providers.apple import APPLE_PROVIDER_NAME
from wybra.providers.github import GITHUB_PROVIDER_NAME
from wybra.providers.google import GOOGLE_PROVIDER_NAME
from wybra.template import render_page

from .shared import (
    _identity_context,
    _identity_options,
    _load_active_totp_credential_id,
    _load_user_by_id,
    _persistence_scope_from_request,
    _public_signup_enabled,
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
        context = _identity_context(
            request,
            page_title="Create account",
            form=SignupCommandForm(),
        )
        return await render_page(request, "identity/pages/signup.html", context)

    form = SignupCommandForm()
    await form.parse(await request_form_data(request))
    email = command_text(form, "email").strip()
    password = command_text(form, "password")
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
        form=form,
    )
    return await render_page(
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
        return await render_page(request, "identity/pages/logout.html", context)

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
        return await render_page(request, "identity/pages/account.html", context)

    return await render_page(request, "identity/pages/account.html", context)


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
    return await _unlink_provider_response(
        request,
        user,
        provider_name=GOOGLE_PROVIDER_NAME,
        provider_label=_provider_label(GOOGLE_PROVIDER_NAME),
    )


@account_router.post(
    "/security/providers/github/unlink",
    include_in_schema=False,
    name="auth:security-github-unlink",
)
async def unlink_github_provider(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    return await _unlink_provider_response(
        request,
        user,
        provider_name=GITHUB_PROVIDER_NAME,
        provider_label=_provider_label(GITHUB_PROVIDER_NAME),
    )


@account_router.post(
    "/security/providers/apple/unlink",
    include_in_schema=False,
    name="auth:security-apple-unlink",
)
async def unlink_apple_provider(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    return await _unlink_provider_response(
        request,
        user,
        provider_name=APPLE_PROVIDER_NAME,
        provider_label=_provider_label(APPLE_PROVIDER_NAME),
    )


async def _unlink_provider_response(
    request: Request,
    user: User,
    *,
    provider_name: str,
    provider_label: str,
) -> Response:
    form = ProviderUnlinkCommandForm()
    await form.parse(await request_form_data(request))
    provider_id = command_text(form, "provider_id")
    scope_factory = _persistence_scope_from_request(request)
    error: str | None = None
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")

        store = _provider_credential_store(request, scope)
        provider = await store.get_user_provider_by_id(
            user_id=db_user.id,
            provider_id=provider_id,
        )
        if provider is None or provider.provider_name != provider_name:
            await publish_credential_access(
                request,
                operation="unlink",
                provider=provider_name,
                outcome="rejected",
                user_id=db_user.id,
                email=db_user.email,
            )
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if not await _user_has_usable_account_sign_in(
            request,
            scope,
            db_user,
            exclude_provider_id=provider.id,
        ):
            error = f"Add another sign-in method before unlinking {provider_label}."
            await publish_credential_access(
                request,
                operation="unlink",
                provider=provider_name,
                outcome="rejected",
                user_id=db_user.id,
                email=db_user.email,
            )
        else:
            await store.unlink_user_provider(
                user_id=db_user.id,
                provider_id=provider.id,
            )
            await publish_credential_access(
                request,
                operation="unlink",
                provider=provider_name,
                outcome="succeeded",
                user_id=db_user.id,
                email=db_user.email,
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
    scope_factory = _persistence_scope_from_request(request)
    error: str | None = None
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")

        if not _local_password_login_usable(db_user):
            await publish_credential_access(
                request,
                operation="disable",
                provider="password",
                outcome="ignored",
                user_id=db_user.id,
                email=db_user.email,
            )
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if not await _user_has_usable_account_sign_in(
            request,
            scope,
            db_user,
            exclude_password=True,
        ):
            error = "Add another sign-in method before disabling password sign-in."
            await publish_credential_access(
                request,
                operation="disable",
                provider="password",
                outcome="rejected",
                user_id=db_user.id,
                email=db_user.email,
            )
        else:
            db_user.password_login_enabled = False
            await scope.users.save_user(db_user)
            await publish_credential_access(
                request,
                operation="disable",
                provider="password",
                outcome="succeeded",
                user_id=db_user.id,
                email=db_user.email,
            )

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
        passkeys=await _security_passkey_section(request, user),
        providers=await _security_provider_section(request, user),
        totp=await _security_totp_section(request, user),
    )
    return await render_page(
        request,
        "identity/pages/security.html",
        context,
        status_code=status_code,
    )


async def _security_totp_section(request: Request, user: User) -> dict[str, object]:
    options = _identity_options(request)
    if options.totp_mode == TOTP_DISABLED:
        return {"available": False}

    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        active_credential_id = await _load_active_totp_credential_id(scope, user.id)

    return {
        "available": True,
        "enabled": active_credential_id is not None,
        "setup_path": _totp_setup_path(request),
    }


def _totp_setup_path(request: Request) -> str:
    security_path = _route_path(request, "auth:security")
    setup_query = urlencode({"return_to": security_path})
    return f"{_route_path(request, 'auth:totp-setup')}?{setup_query}"


async def _security_passkey_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    options = _identity_options(request)
    if not _passkeys_effectively_enabled(options):
        return {"available": False}

    register_options_path = _optional_route_path(
        request,
        "auth:passkey-register-options",
    )
    register_complete_path = _optional_route_path(
        request,
        "auth:passkey-register-complete",
    )
    revoke_path = _optional_route_path(request, "auth:security-passkey-revoke")
    if (
        register_options_path is None
        or register_complete_path is None
        or revoke_path is None
    ):
        return {"available": False}

    credentials: list[dict[str, object]] = []
    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        if db_user is None:
            return {"available": False}

        active_credentials = (
            await scope.webauthn_credentials.list_active_webauthn_credentials(
                str(db_user.id)
            )
        )
        for credential in active_credentials:
            credentials.append(
                {
                    "id": credential.id,
                    "label": credential.label or "Passkey",
                    "created_at": credential.created_at,
                    "last_used_at": credential.last_used_at,
                    "backed_up": credential.credential_backed_up,
                    "form": PasskeyRevokeCommandForm(
                        values={"credential_id": credential.id}
                    ),
                }
            )
        email_verified = db_user.is_verified

    return {
        "available": True,
        "add_available": email_verified,
        "verification_required": not email_verified,
        "credentials": tuple(credentials),
        "register_options_path": register_options_path,
        "register_complete_path": register_complete_path,
        "revoke_path": revoke_path,
    }


async def _security_password_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    enabled = _local_password_login_usable(user)
    disable_path = _optional_route_path(request, "auth:security-password-disable")
    disable_available = False
    if enabled and disable_path is not None:
        disable_available = await _security_password_disable_available(request, user)

    return {
        "available": True,
        "enabled": enabled,
        "disable_available": disable_available,
        "disable_path": disable_path,
    }


async def _security_password_disable_available(
    request: Request,
    user: User,
) -> bool:
    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        return bool(
            db_user is not None
            and await _user_has_usable_account_sign_in(
                request,
                scope,
                db_user,
                exclude_password=True,
            )
        )


async def _security_provider_section(
    request: Request,
    user: User,
) -> dict[str, object]:
    sections = []
    for option in _provider_security_options(
        request,
        return_to=_optional_route_path(request, "auth:security"),
    ):
        sections.append(
            await _security_named_provider_section(
                request,
                user,
                provider_name=option["name"],
                provider_label=option["label"],
                link_path=option["link_path"],
                unlink_path=option["unlink_path"],
            )
        )
    providers = tuple(section for section in sections if section["available"])
    return {
        "available": bool(providers),
        "providers": providers,
    }


async def _security_named_provider_section(
    request: Request,
    user: User,
    *,
    provider_name: str,
    provider_label: str,
    link_path: str | None,
    unlink_path: str | None,
) -> dict[str, object]:
    if link_path is None or unlink_path is None:
        return {"available": False}

    linked_accounts: list[dict[str, str]] = []
    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        store = _provider_credential_store(request, scope)
        providers = await store.get_user_providers(
            user_id=user.id,
            provider_name=provider_name,
        )
        for provider in providers:
            if not provider.provider_enabled:
                continue
            linked_accounts.append(
                {
                    "id": str(provider.id),
                    "account_email": provider.account_email,
                    "form": ProviderUnlinkCommandForm(
                        values={"provider_id": str(provider.id)}
                    ),
                }
            )

    return {
        "available": True,
        "name": provider_name,
        "label": provider_label,
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
        context = _identity_context(
            request,
            page_title="Reset password",
            request_form=PasswordResetRequestCommandForm(),
            confirm_form=PasswordResetConfirmCommandForm(),
        )
        return await render_page(request, "identity/pages/password_reset.html", context)

    form = PasswordResetRequestCommandForm()
    await form.parse(await request_form_data(request))
    email = command_text(form, "email").strip()
    await request_password_reset(request, email)
    context = _identity_context(
        request,
        page_title="Reset password",
        email=email,
        form_message="If the account exists, a reset link has been queued.",
        request_form=form,
        confirm_form=PasswordResetConfirmCommandForm(),
    )
    return await render_page(request, "identity/pages/password_reset.html", context)


@account_router.post(
    "/password/reset/confirm",
    include_in_schema=False,
    name="auth:password-reset-confirm",
)
async def password_reset_confirm(request: Request) -> Response:
    form = PasswordResetConfirmCommandForm()
    await form.parse(await request_form_data(request))
    token = command_text(form, "token")
    password = command_text(form, "password")
    did_reset = await reset_password(request, token, password)
    context = _identity_context(
        request,
        page_title="Reset password",
        form_message="Password reset complete." if did_reset else None,
        form_error=None if did_reset else "The reset token is invalid or expired.",
        request_form=PasswordResetRequestCommandForm(),
        confirm_form=form,
    )
    return await render_page(
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
        context = _identity_context(
            request,
            page_title="Verify email",
            request_form=VerificationRequestCommandForm(),
            confirm_form=VerificationConfirmCommandForm(),
        )
        return await render_page(request, "identity/pages/verify.html", context)

    form = VerificationRequestCommandForm()
    await form.parse(await request_form_data(request))
    email = command_text(form, "email").strip()
    await request_verification(request, email)
    context = _identity_context(
        request,
        page_title="Verify email",
        email=email,
        form_message=(
            "If the account can be verified, a verification link has been queued."
        ),
        request_form=form,
        confirm_form=VerificationConfirmCommandForm(),
    )
    return await render_page(request, "identity/pages/verify.html", context)


@account_router.post(
    "/verify/confirm",
    include_in_schema=False,
    name="auth:verify-confirm",
)
async def verify_confirm(request: Request) -> Response:
    form = VerificationConfirmCommandForm()
    await form.parse(await request_form_data(request))
    token = command_text(form, "token")
    verification_result = await verify_user(request, token)
    did_verify = verification_result.is_ok()
    options = _identity_options(request)
    totp_setup_required = False
    if did_verify and options.totp_mode == TOTP_REQUIRED and verification_result.value:
        scope_factory = _persistence_scope_from_request(request)
        async with scope_factory() as scope:
            totp_setup_required = (
                await _load_active_totp_credential_id(scope, verification_result.value)
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
        request_form=VerificationRequestCommandForm(),
        confirm_form=form,
    )
    return await render_page(
        request,
        "identity/pages/verify.html",
        context,
        status_code=200 if did_verify else 400,
    )
