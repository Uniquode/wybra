from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from wybra.auth.ids import log_safe_uuid
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    TOTP_PENDING_STATUS,
    SqlAlchemyChallengeStore,
)
from wybra.auth.mfa.totp import generate_totp_secret, totp_auth_uri
from wybra.auth.options import TOTP_REQUIRED
from wybra.auth.provider_support import (
    user_has_usable_account_sign_in as _user_has_usable_account_sign_in,
)
from wybra.auth.result import ERROR_TOTP_SETUP_REQUIRED
from wybra.auth.routes.paths import normalise_return_to
from wybra.auth.routes.paths import route_path as _route_path
from wybra.auth.routes.totp import (
    RECOVERY_CODES_DOWNLOAD_FILENAME,
    TOTP_SETUP_PAGE_MESSAGES,
    clear_totp_setup_nonce_cookie,
    ensure_totp_setup_supported,
    is_totp_setup_challenge,
    recovery_code_store,
    recovery_codes_download_href,
    render_totp_setup_page,
    setup_totp_error_page,
    totp_assertion,
    totp_credential_store,
    totp_issuer,
    totp_required_methods,
    totp_setup_nonce_valid,
    verify_totp_code_for_credential,
)
from wybra.auth.sessions import (
    complete_authentication_ceremony,
    resolve_current_user,
    set_session_cookie,
)
from wybra.auth.timestamps import current_timestamp
from wybra.forms import request_form_data
from wybra.template import render_page

from .shared import (
    _form_value,
    _fresh_primary_assertion_satisfied,
    _fresh_security_assertion_satisfied,
    _fresh_single_security_assertion_satisfied,
    _identity_options,
    _is_effectively_active_user,
    _load_user_by_id,
    _login_error_response,
    _require_authenticated_user,
    _session_factory_from_request,
    _totp_setup_return_to,
    account_router,
    logger,
)

TOTP_CONFIRMATION_MESSAGE = (
    "Confirm this action with your password, authenticator code, or a recovery code."
)
TOTP_LAST_METHOD_MESSAGE = (
    "Add another sign-in method before disabling authenticator verification."
)


@account_router.api_route(
    "/totp/setup",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:totp-setup",
)
async def totp_setup(request: Request) -> Response:
    ensure_totp_setup_supported(request)
    options = _identity_options(request)
    return_to = normalise_return_to(
        request.query_params.get("return_to"),
        default="/account",
    )
    setup_challenge_id = request.query_params.get("setup_challenge_id", "").strip()
    setup_code = ""

    if request.method == "POST":
        form_data = await request_form_data(request)
        return_to = normalise_return_to(
            _form_value(form_data, "return_to"),
            default=return_to,
        )
        setup_challenge_id = _form_value(
            form_data,
            "setup_challenge_id",
            default=setup_challenge_id,
        )
        setup_code = _form_value(form_data, "setup_totp_code").strip()

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        authenticated_user = await resolve_current_user(request)
        challenge_store = SqlAlchemyChallengeStore(session)
        setup_challenge = None
        if setup_challenge_id:
            challenge = await challenge_store.get_challenge(setup_challenge_id)
            if challenge is None:
                await session.commit()
            if (
                challenge is not None
                and is_totp_setup_challenge(challenge)
                and totp_setup_nonce_valid(request, challenge)
            ):
                setup_challenge = challenge
                return_to = _totp_setup_return_to(
                    setup_challenge.metadata_payload,
                    default=return_to,
                )

        challenge_user = (
            await _load_user_by_id(session, setup_challenge.user_id)
            if setup_challenge is not None
            else None
        )
        if authenticated_user is not None and challenge_user is not None:
            if str(authenticated_user.id) != str(challenge_user.id):
                logger.warning(
                    "TOTP setup challenge user mismatch: "
                    "authenticated_user_id=%s challenge_user_id=%s challenge_id=%s",
                    log_safe_uuid(authenticated_user.id),
                    log_safe_uuid(challenge_user.id),
                    log_safe_uuid(setup_challenge_id),
                )
                setup_challenge = None
                challenge_user = None

        user = authenticated_user if authenticated_user is not None else challenge_user
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        if not _is_effectively_active_user(user):
            raise HTTPException(status_code=401, detail="Account is inactive.")

        if not user.is_verified:
            return setup_totp_error_page(
                request,
                return_to=return_to,
                setup_challenge_id=setup_challenge_id or "",
                setup_error=TOTP_SETUP_PAGE_MESSAGES["verify_email"],
            )

        store = totp_credential_store(request, session)
        pending_credential_id = await store.get_pending_totp_credential(str(user.id))
        if pending_credential_id is None:
            pending_credential_id = await store.create_pending_totp_credential(
                str(user.id),
                generate_totp_secret(),
            )
            await session.commit()

        credential = await store.get_totp_credential(pending_credential_id)
        if credential is None:
            raise HTTPException(
                status_code=500,
                detail=TOTP_SETUP_PAGE_MESSAGES["initialise_error"],
            )

        totp_uri = totp_auth_uri(
            account_name=user.email,
            secret=store.decrypt_totp_secret(credential),
            issuer=totp_issuer(request),
            period=options.totp_period_seconds,
        )
        setup_totp_secret = store.decrypt_totp_secret(credential)

        if request.method != "POST":
            return render_totp_setup_page(
                request,
                return_to=return_to,
                setup_challenge_id=setup_challenge_id or "",
                setup_totp_secret=setup_totp_secret,
                setup_totp_uri=totp_uri,
            )

        if not setup_code:
            return setup_totp_error_page(
                request,
                return_to=return_to,
                setup_challenge_id=setup_challenge_id or "",
                setup_totp_secret=setup_totp_secret,
                setup_totp_uri=totp_uri,
                setup_error=TOTP_SETUP_PAGE_MESSAGES["code_required"],
            )

        verification_timestamp = current_timestamp()
        (
            accepted,
            counter,
            setup_challenge_error,
        ) = await verify_totp_code_for_credential(
            session=session,
            store=store,
            credential_id=str(credential.id),
            user_id=str(user.id),
            code=setup_code,
            options=options,
            expected_status=TOTP_PENDING_STATUS,
            timestamp=verification_timestamp,
        )
        if not accepted or counter is None:
            return setup_totp_error_page(
                request,
                return_to=return_to,
                setup_challenge_id=setup_challenge_id or "",
                setup_totp_secret=setup_totp_secret,
                setup_totp_uri=totp_uri,
                setup_error=(
                    setup_challenge_error or TOTP_SETUP_PAGE_MESSAGES["invalid_code"]
                ),
            )

        await store.activate_totp_credential(str(credential.id))
        recovery_store = recovery_code_store(request, session)
        recovery_codes = generate_recovery_codes()
        await recovery_store.replace_recovery_codes(
            str(user.id),
            str(credential.id),
            recovery_codes,
        )
        if setup_challenge is not None:
            await challenge_store.consume_challenge(setup_challenge.id)

        await session.commit()

        response = render_totp_setup_page(
            request,
            return_to=return_to,
            setup_challenge_id="",
            setup_totp_secret="",
            setup_totp_uri="",
            setup_complete=True,
            recovery_codes=recovery_codes,
        )

        if setup_challenge is not None and options.totp_mode == TOTP_REQUIRED:
            ceremony_result = await complete_authentication_ceremony(
                request,
                user,
                ceremony_id=setup_challenge.id,
                required_methods=totp_required_methods(
                    options,
                    has_active_totp=True,
                ),
                assertions=(
                    totp_assertion(
                        str(user.id),
                        ceremony_id=setup_challenge.id,
                        asserted_at=verification_timestamp,
                    ),
                ),
            )
            if ceremony_result.is_failure() or ceremony_result.value is None:
                return _login_error_response(
                    request,
                    email=user.email,
                    return_to=return_to,
                    status_code=401,
                    message=ERROR_TOTP_SETUP_REQUIRED,
                )

            set_session_cookie(
                response,
                request,
                ceremony_result.value,
                options,
            )
            clear_totp_setup_nonce_cookie(response, request)

        return response


@account_router.api_route(
    "/totp/disable",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:totp-disable",
)
async def disable_totp(request: Request) -> Response:
    ensure_totp_setup_supported(request)
    user = await _require_authenticated_user(request)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        store = totp_credential_store(request, session)
        active_credential_id = await store.get_active_totp_credential(str(user.id))
        if active_credential_id is None:
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if request.method != "POST":
            return _totp_action_confirmation_page(
                request,
                page_title="Disable authenticator",
                action_path=str(request.url_for("auth:totp-disable")),
                button_label="Disable authenticator",
            )

        if not await _user_has_usable_account_sign_in(request, session, user):
            return _totp_action_confirmation_page(
                request,
                page_title="Disable authenticator",
                action_path=str(request.url_for("auth:totp-disable")),
                button_label="Disable authenticator",
                error=TOTP_LAST_METHOD_MESSAGE,
                status_code=400,
            )

        if not await _fresh_security_assertion_satisfied(
            request,
            user,
            session,
            active_credential_id=active_credential_id,
        ):
            return _totp_action_confirmation_page(
                request,
                page_title="Disable authenticator",
                action_path=str(request.url_for("auth:totp-disable")),
                button_label="Disable authenticator",
                error=TOTP_CONFIRMATION_MESSAGE,
                status_code=400,
            )

        await store.disable_totp_credential(active_credential_id)
        await session.commit()

    return RedirectResponse(url=_route_path(request, "auth:security"), status_code=303)


@account_router.api_route(
    "/totp/recovery-codes/regenerate",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:totp-recovery-codes-regenerate",
)
async def regenerate_totp_recovery_codes(request: Request) -> Response:
    ensure_totp_setup_supported(request)
    user = await _require_authenticated_user(request)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        store = totp_credential_store(request, session)
        active_credential_id = await store.get_active_totp_credential(str(user.id))
        if active_credential_id is None:
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if request.method != "POST":
            return _totp_action_confirmation_page(
                request,
                page_title="Generate replacement recovery codes",
                action_path=str(request.url_for("auth:totp-recovery-codes-regenerate")),
                button_label="Generate recovery codes",
                single_confirmation_field=True,
            )

        if not await _fresh_single_security_assertion_satisfied(
            request,
            user,
            session,
            active_credential_id=active_credential_id,
        ):
            return _totp_action_confirmation_page(
                request,
                page_title="Generate replacement recovery codes",
                action_path=str(request.url_for("auth:totp-recovery-codes-regenerate")),
                button_label="Generate recovery codes",
                error=TOTP_CONFIRMATION_MESSAGE,
                single_confirmation_field=True,
                status_code=400,
            )

        recovery_codes = generate_recovery_codes()
        recovery_store = recovery_code_store(request, session)
        await recovery_store.replace_recovery_codes(
            str(user.id),
            active_credential_id,
            recovery_codes,
        )
        await session.commit()

    return _totp_action_confirmation_page(
        request,
        page_title="Recovery codes",
        action_path=str(request.url_for("auth:totp-recovery-codes-regenerate")),
        button_label="Generate recovery codes",
        recovery_codes=recovery_codes,
    )


def _totp_action_confirmation_page(
    request: Request,
    *,
    page_title: str,
    action_path: str,
    button_label: str,
    error: str | None = None,
    recovery_codes: tuple[str, ...] = (),
    single_confirmation_field: bool = False,
    status_code: int = 200,
) -> Response:
    return render_page(
        request,
        "identity/pages/totp_confirmation.html",
        {
            "page_title": page_title,
            "action_path": action_path,
            "button_label": button_label,
            "confirmation_message": TOTP_CONFIRMATION_MESSAGE,
            "confirmation_error": error,
            "single_confirmation_field": single_confirmation_field,
            "recovery_codes": recovery_codes,
            "recovery_codes_download_href": recovery_codes_download_href(
                recovery_codes
            ),
            "recovery_codes_download_filename": RECOVERY_CODES_DOWNLOAD_FILENAME,
        },
        status_code=status_code,
    )


@account_router.post(
    "/totp/reset",
    include_in_schema=False,
    name="auth:totp-reset",
)
async def reset_totp(request: Request) -> Response:
    ensure_totp_setup_supported(request)
    user = await _require_authenticated_user(request)
    if not await _fresh_primary_assertion_satisfied(request, user):
        return RedirectResponse(url="/account", status_code=303)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        store = totp_credential_store(request, session)
        await store.clear_totp_credentials(str(user.id))
        await session.commit()

    options = _identity_options(request)
    redirect_to = (
        "/account/totp/setup"
        if options.totp_mode == TOTP_REQUIRED and user.is_verified
        else "/account"
    )
    return RedirectResponse(url=redirect_to, status_code=303)
