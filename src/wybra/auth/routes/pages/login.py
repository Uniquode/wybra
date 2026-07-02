from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from wybra.auth.ids import log_safe_uuid
from wybra.auth.mfa.recovery import RECOVERY_CODE_LENGTH
from wybra.auth.mfa.storage import TOTP_ACTIVE_STATUS, SqlAlchemyChallengeStore
from wybra.auth.mfa.totp import is_valid_totp_code
from wybra.auth.models import User
from wybra.auth.options import TOTP_DISABLED, TOTP_REQUIRED
from wybra.auth.result import (
    ERROR_EMAIL_VERIFICATION_REQUIRED,
    ERROR_TOTP_CODE_REQUIRED,
    ERROR_TOTP_INVALID,
    ERROR_TOTP_SETUP_REQUIRED,
    ERROR_VERIFICATION_CODE_INVALID,
)
from wybra.auth.routes.paths import normalise_return_to
from wybra.auth.routes.totp import (
    TOTP_SETUP_BYPASS_TOKEN,
    clear_totp_login_nonce_cookie,
    clear_totp_setup_nonce_cookie,
    is_totp_setup_challenge,
    recovery_code_store,
    render_totp_login_challenge,
    render_totp_setup_prompt,
    set_totp_login_nonce_cookie,
    set_totp_setup_nonce_cookie,
    totp_assertion,
    totp_credential_problem,
    totp_credential_store,
    totp_login_nonce_valid,
    totp_required_methods,
    totp_setup_nonce_valid,
    verify_totp_code_for_credential,
)
from wybra.auth.sessions import (
    authenticate_user,
    complete_authentication_ceremony,
    set_session_cookie,
)
from wybra.auth.timestamps import current_timestamp
from wybra.forms import request_form_data
from wybra.template import render_page

from .shared import (
    _complete_login_ceremony,
    _create_totp_login_challenge,
    _create_totp_setup_challenge,
    _form_value,
    _identity_options,
    _is_effectively_active_user,
    _load_active_totp_credential_id,
    _load_user_by_id,
    _login_error_response,
    _login_page_context,
    _session_factory_from_request,
    _totp_login_error_response,
    _totp_setup_return_to,
    _verification_required_response,
    account_router,
    logger,
)


@account_router.api_route(
    "/login",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:login",
)
async def login(request: Request) -> Response:
    if request.method == "GET":
        challenge_id = request.query_params.get("challenge_id", "").strip()
        if request.query_params.get("challenge_step") == "totp" and challenge_id:
            return render_totp_login_challenge(
                request,
                return_to=normalise_return_to(request.query_params.get("return_to")),
                email=request.query_params.get("email", "").strip(),
                challenge_id=challenge_id,
            )

        return render_page(
            request,
            "identity/pages/login.html",
            _login_page_context(
                request,
                return_to=normalise_return_to(request.query_params.get("return_to")),
            ),
        )

    form_data = await request_form_data(request)
    email = _form_value(form_data, "email").strip()
    password = _form_value(form_data, "password")
    return_to = normalise_return_to(_form_value(form_data, "return_to"))
    challenge_id = _form_value(form_data, "challenge_id")
    if not challenge_id:
        challenge_id = _form_value(form_data, "setup_challenge_id")
    setup_bypass = _form_value(form_data, TOTP_SETUP_BYPASS_TOKEN).lower() in (
        "1",
        "true",
    )

    if challenge_id:
        return await _handle_login_totp_challenge(
            request,
            challenge_id=challenge_id,
            return_to=return_to,
            email=email,
            password=password,
            submitted_code=_submitted_totp_code_value(form_data),
            setup_bypass=setup_bypass,
        )

    return await _handle_primary_login(
        request,
        email=email,
        password=password,
        return_to=return_to,
    )


def _submitted_totp_code_value(form_data: FormData) -> str:
    totp_code = _form_value(form_data, "totp_code").strip()
    if totp_code:
        return totp_code

    return _form_value(form_data, "recovery_code").strip()


async def _handle_primary_login(
    request: Request,
    *,
    email: str,
    password: str,
    return_to: str,
) -> Response:
    user = await authenticate_user(request, email, password)
    if user is None:
        return _login_error_response(
            request,
            email=email,
            return_to=return_to,
        )

    return await _handle_totp_post_authentication_decision(
        request,
        user=user,
        email=email,
        return_to=return_to,
    )


async def _handle_totp_post_authentication_decision(
    request: Request,
    *,
    user: User,
    email: str,
    return_to: str,
) -> Response:
    options = _identity_options(request)
    if options.totp_mode == TOTP_DISABLED:
        return await _complete_login_ceremony(request, user, return_to=return_to)

    session_factory = _session_factory_from_request(request)
    async with session_factory() as session:
        credential_id = await _load_active_totp_credential_id(session, user.id)
        if credential_id:
            challenge_id, login_nonce = await _create_totp_login_challenge(
                request,
                user_id=str(user.id),
                credential_id=credential_id,
            )
            response = render_totp_login_challenge(
                request,
                return_to=return_to,
                email=email,
                challenge_id=challenge_id,
            )
            set_totp_login_nonce_cookie(response, request, login_nonce)
            return response

        if options.totp_mode == TOTP_REQUIRED:
            setup_challenge_id, setup_nonce = await _create_totp_setup_challenge(
                request,
                user_id=str(user.id),
                return_to=return_to,
            )
            response = render_totp_setup_prompt(
                request,
                return_to=return_to,
                email=email,
                setup_challenge_id=setup_challenge_id,
                setup_bypass_error=(
                    "Verify your email before authenticator setup can proceed."
                    if not user.is_verified
                    else None
                ),
            )
            set_totp_setup_nonce_cookie(response, request, setup_nonce)
            return response

    return await _complete_login_ceremony(request, user, return_to=return_to)


async def _handle_login_totp_challenge(
    request: Request,
    *,
    challenge_id: str,
    return_to: str,
    email: str,
    password: str,
    submitted_code: str,
    setup_bypass: bool = False,
) -> Response:
    del password
    options = _identity_options(request)
    session_factory = _session_factory_from_request(request)
    challenge_step = "totp"

    async with session_factory() as session:
        challenge_store = SqlAlchemyChallengeStore(session)
        challenge = await challenge_store.get_challenge(challenge_id)
        if challenge is None:
            await session.commit()
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_TOTP_CODE_REQUIRED,
                challenge_step=challenge_step,
            )

        if is_totp_setup_challenge(challenge):
            challenge_step = "setup"
            challenge_user = await _load_user_by_id(session, challenge.user_id)
            now = current_timestamp()
            if (
                challenge_user is None
                or not _is_effectively_active_user(
                    challenge_user,
                    now=now,
                )
                or not totp_setup_nonce_valid(request, challenge)
            ):
                return _totp_login_error_response(
                    request,
                    email=email,
                    return_to=return_to,
                    status_code=401,
                    message=ERROR_TOTP_CODE_REQUIRED,
                    challenge_step=challenge_step,
                )

            challenge_return_to = _totp_setup_return_to(
                challenge.metadata_payload,
                default=return_to,
            )
            if setup_bypass:
                await challenge_store.consume_challenge(challenge_id)
                await session.commit()

                ceremony_result = await complete_authentication_ceremony(
                    request,
                    challenge_user,
                )
                if (
                    ceremony_result.is_failure()
                    and ceremony_result.error_type == ERROR_EMAIL_VERIFICATION_REQUIRED
                ):
                    response = _verification_required_response(
                        request,
                        email=challenge_user.email,
                    )
                    clear_totp_setup_nonce_cookie(response, request)
                    return response

                if ceremony_result.is_failure() or ceremony_result.value is None:
                    return _totp_login_error_response(
                        request,
                        email=challenge_user.email,
                        return_to=challenge_return_to,
                        status_code=401,
                        message=ERROR_TOTP_SETUP_REQUIRED,
                        challenge_step=challenge_step,
                    )

                response = RedirectResponse(
                    url=challenge_return_to,
                    status_code=303,
                )
                set_session_cookie(
                    response,
                    request,
                    ceremony_result.value,
                    _identity_options(request),
                )
                clear_totp_setup_nonce_cookie(response, request)
                return response

            return render_totp_setup_prompt(
                request,
                return_to=challenge_return_to,
                email=challenge_user.email,
                setup_challenge_id=challenge.id,
                setup_bypass_error=(
                    "Verify your email before authenticator setup can proceed."
                    if not challenge_user.is_verified
                    else None
                ),
            )

        if challenge.kind != "totp":
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_TOTP_INVALID,
                challenge_step=challenge_step,
            )

        user = await _load_user_by_id(session, challenge.user_id)
        if user is None:
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_TOTP_INVALID,
                challenge_step=challenge_step,
            )

        if not totp_login_nonce_valid(request, challenge):
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_VERIFICATION_CODE_INVALID,
                challenge_id=challenge_id,
                challenge_step=challenge_step,
            )

        challenge_return_to = (
            challenge.metadata_payload.get("return_to")
            if isinstance(challenge.metadata_payload, dict)
            else None
        )
        if isinstance(challenge_return_to, str) and challenge_return_to.strip():
            return_to = normalise_return_to(challenge_return_to, default=return_to)

        credential_id = None
        if isinstance(challenge.metadata_payload, dict):
            maybe_credential_id = challenge.metadata_payload.get("totp_credential_id")
            if isinstance(maybe_credential_id, str):
                credential_id = maybe_credential_id

        if credential_id is None:
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_TOTP_INVALID,
                challenge_id=challenge.id,
                challenge_step=challenge_step,
            )

        store = totp_credential_store(request, session)
        credential = await store.get_totp_credential(credential_id)
        credential_problem = totp_credential_problem(
            credential,
            expected_user_id=str(user.id),
            expected_status=TOTP_ACTIVE_STATUS,
        )
        if credential_problem is not None:
            logger.warning(
                "TOTP challenge credential rejected: challenge_id=%s "
                "credential_id=%s user_id=%s reason=%s",
                log_safe_uuid(challenge_id),
                log_safe_uuid(credential_id),
                log_safe_uuid(user.id),
                credential_problem,
            )
            await challenge_store.consume_challenge(challenge_id)
            await session.commit()
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=401,
                message=ERROR_VERIFICATION_CODE_INVALID,
                challenge_id=challenge_id,
                challenge_step=challenge_step,
            )

        code = (submitted_code or "").strip()
        totp_asserted_at: float | None = None
        if is_valid_totp_code(code):
            verification_timestamp = current_timestamp()
            accepted, counter, _challenge_error = await verify_totp_code_for_credential(
                session=session,
                store=store,
                credential_id=credential_id,
                user_id=str(user.id),
                code=code,
                options=options,
                timestamp=verification_timestamp,
            )
            if not accepted or counter is None:
                return _totp_login_error_response(
                    request,
                    email=email,
                    return_to=return_to,
                    status_code=401,
                    message=ERROR_VERIFICATION_CODE_INVALID,
                    challenge_id=challenge_id,
                    challenge_step=challenge_step,
                )

            totp_asserted_at = verification_timestamp
        elif code:
            recovery_code = code.upper()
            if len(recovery_code) != RECOVERY_CODE_LENGTH:
                return _totp_login_error_response(
                    request,
                    email=email,
                    return_to=return_to,
                    status_code=401,
                    message=ERROR_VERIFICATION_CODE_INVALID,
                    challenge_id=challenge_id,
                    challenge_step=challenge_step,
                )

            recovery_store = recovery_code_store(request, session)
            if not await recovery_store.consume_recovery_code(
                str(user.id),
                recovery_code,
            ):
                return _totp_login_error_response(
                    request,
                    email=email,
                    return_to=return_to,
                    status_code=401,
                    message=ERROR_VERIFICATION_CODE_INVALID,
                    challenge_id=challenge_id,
                    challenge_step=challenge_step,
                )
            totp_asserted_at = current_timestamp()
        else:
            return _totp_login_error_response(
                request,
                email=email,
                return_to=return_to,
                status_code=400,
                message=ERROR_TOTP_CODE_REQUIRED,
                challenge_id=challenge_id,
                challenge_step=challenge_step,
            )

        await challenge_store.consume_challenge(challenge_id)
        await session.commit()

    ceremony_result = await complete_authentication_ceremony(
        request,
        user,
        ceremony_id=challenge_id,
        required_methods=totp_required_methods(
            _identity_options(request),
            has_active_totp=True,
        ),
        assertions=(
            totp_assertion(
                str(user.id),
                ceremony_id=challenge_id,
                asserted_at=totp_asserted_at,
            ),
        )
        if totp_asserted_at is not None
        else (),
    )
    if ceremony_result.is_failure() or ceremony_result.value is None:
        if (
            ceremony_result.is_failure()
            and ceremony_result.error_type == ERROR_EMAIL_VERIFICATION_REQUIRED
        ):
            response = _verification_required_response(request, email=user.email)
            clear_totp_login_nonce_cookie(response, request)
            return response

        return _totp_login_error_response(
            request,
            email=email,
            return_to=return_to,
            status_code=401,
            message=ERROR_TOTP_INVALID,
            challenge_step=challenge_step,
        )

    response = RedirectResponse(url=return_to, status_code=303)
    set_session_cookie(
        response, request, ceremony_result.value, _identity_options(request)
    )
    clear_totp_login_nonce_cookie(response, request)
    return response
