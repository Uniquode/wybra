from __future__ import annotations

import logging
from collections.abc import Mapping
from json import JSONDecodeError
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from wybra.auth.capabilities import login_required
from wybra.auth.events import publish_credential_access
from wybra.auth.forms import PasskeyRevokeCommandForm, command_text
from wybra.auth.mfa.storage import (
    WEBAUTHN_ACTIVE_STATUS,
)
from wybra.auth.mfa.webauthn import (
    WEBAUTHN_LOGIN_PURPOSE,
    WEBAUTHN_REGISTRATION_PURPOSE,
    WebAuthnCeremonyError,
    challenge_from_metadata,
    challenge_has_purpose,
    passkey_authentication_options,
    passkey_registration_options,
    passkeys_effectively_enabled,
    verify_passkey_authentication,
    verify_passkey_registration,
    webauthn_assertion,
    webauthn_challenge_metadata,
)
from wybra.auth.models import User
from wybra.auth.options import TOTP_DISABLED
from wybra.auth.persistence.contracts import AuthPersistenceError
from wybra.auth.provider_support import user_has_usable_account_sign_in
from wybra.auth.result import (
    ERROR_AUTHENTICATION_METHOD_REQUIRED,
    ERROR_EMAIL_VERIFICATION_REQUIRED,
)
from wybra.auth.routes.paths import normalise_return_to
from wybra.auth.routes.paths import route_path as _route_path
from wybra.auth.routes.totp import (
    set_totp_login_nonce_cookie,
    totp_required_methods,
)
from wybra.auth.sessions import complete_authentication_ceremony, set_session_cookie
from wybra.auth.timestamps import current_timestamp
from wybra.forms import request_form_data

from .account import _security_page_response
from .shared import (
    _identity_options,
    _is_effectively_active_user,
    _load_user_by_id,
    _persistence_scope_from_request,
    account_router,
)

logger = logging.getLogger(__name__)
LOGIN_REQUIRED = Depends(login_required)
PASSKEY_GENERIC_ERROR = "Passkey verification failed."
PASSKEY_UNAVAILABLE_ERROR = "Passkey login is not available."


@account_router.post(
    "/security/passkeys/register/options",
    include_in_schema=False,
    name="auth:passkey-register-options",
)
async def passkey_register_options(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> JSONResponse:
    _ensure_passkeys_supported(request)
    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        db_user_id = str(db_user.id)
        if not db_user.is_verified:
            return _json_error(
                "Verify your email before adding a passkey.",
                status_code=403,
            )

        existing_credentials = (
            await scope.webauthn_credentials.list_active_webauthn_credentials(
                db_user_id,
            )
        )
        options = passkey_registration_options(
            _identity_options(request),
            user_id=db_user_id,
            user_name=db_user.email,
            user_display_name=db_user.email,
            exclude_credentials=existing_credentials,
        )
        challenge = await scope.challenges.create_challenge(
            user_id=db_user_id,
            kind="webauthn",
            expires_at=current_timestamp()
            + _identity_options(request).passkey_timeout_seconds,
            metadata=webauthn_challenge_metadata(
                purpose=WEBAUTHN_REGISTRATION_PURPOSE,
                challenge=options.challenge,
                user_handle=db_user.id.bytes,
            ),
        )

    return JSONResponse(
        {
            "challenge_id": challenge.id,
            "publicKey": options.public_key,
        }
    )


@account_router.post(
    "/security/passkeys/register/complete",
    include_in_schema=False,
    name="auth:passkey-register-complete",
)
async def passkey_register_complete(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> JSONResponse:
    _ensure_passkeys_supported(request)
    payload = await _request_payload(request)
    challenge_id = _payload_text(payload, "challenge_id")
    credential = _payload_mapping(payload, "credential")
    label = _payload_text(payload, "label")
    if challenge_id is None or credential is None:
        return _json_error(PASSKEY_GENERIC_ERROR)

    scope_factory = _persistence_scope_from_request(request)
    db_user_id: str | None = None
    try:
        async with scope_factory() as scope:
            db_user = await _load_user_by_id(scope, user.id)
            if db_user is None:
                raise HTTPException(status_code=401, detail="Authentication required.")
            db_user_id = str(db_user.id)
            if not db_user.is_verified:
                return _json_error(
                    "Verify your email before adding a passkey.",
                    status_code=403,
                )

            challenge = await scope.challenges.get_challenge(challenge_id)
            if (
                challenge is None
                or challenge.kind != "webauthn"
                or challenge.user_id != db_user_id
                or not challenge_has_purpose(
                    challenge.metadata_payload,
                    WEBAUTHN_REGISTRATION_PURPOSE,
                )
            ):
                return _json_error(PASSKEY_GENERIC_ERROR)

            expected_challenge = challenge_from_metadata(challenge.metadata_payload)
            if expected_challenge is None:
                return _json_error(PASSKEY_GENERIC_ERROR)

            verified = verify_passkey_registration(
                _identity_options(request),
                credential=credential,
                expected_challenge=expected_challenge,
            )
            await scope.webauthn_credentials.store_webauthn_credential(
                db_user_id,
                verified.credential_id,
                verified.public_key,
                verified.sign_count,
                label=label,
                user_verified=verified.user_verified,
                credential_device_type=verified.credential_device_type,
                credential_backed_up=verified.credential_backed_up,
                transports=_credential_transports(credential),
                aaguid=verified.aaguid,
                attestation_format=verified.attestation_format,
            )
            await scope.challenges.consume_challenge(challenge_id)
    except (AuthPersistenceError, WebAuthnCeremonyError) as exc:
        async with scope_factory() as scope:
            await scope.challenges.consume_challenge(challenge_id)
        logger.warning(
            "Passkey registration rejected: user_id=%s reason=%s",
            db_user_id,
            getattr(exc, "reason", type(exc).__name__),
        )
        await publish_credential_access(
            request,
            operation="register",
            provider="passkey",
            outcome="rejected",
            user_id=db_user_id,
            error=exc,
        )
        return _json_error(PASSKEY_GENERIC_ERROR)

    await publish_credential_access(
        request,
        operation="register",
        provider="passkey",
        outcome="succeeded",
        user_id=db_user_id,
    )
    return JSONResponse(
        {
            "status": "registered",
            "redirect_to": _route_path(request, "auth:security"),
        }
    )


@account_router.post(
    "/login/passkey/options",
    include_in_schema=False,
    name="auth:passkey-login-options",
)
async def passkey_login_options(request: Request) -> JSONResponse:
    _ensure_passkeys_supported(request)
    payload = await _request_payload(request)
    email = _payload_text(payload, "email")
    return_to = normalise_return_to(_payload_text(payload, "return_to"))
    if email is None:
        return _json_error(PASSKEY_UNAVAILABLE_ERROR, status_code=400)

    scope_factory = _persistence_scope_from_request(request)
    async with scope_factory() as scope:
        user = cast(User | None, await scope.get_user_by_email(email))
        if user is None or not _is_effectively_active_user(user):
            return _json_error(PASSKEY_UNAVAILABLE_ERROR, status_code=400)

        credentials = await scope.webauthn_credentials.list_active_webauthn_credentials(
            str(user.id)
        )
        if not credentials:
            return _json_error(PASSKEY_UNAVAILABLE_ERROR, status_code=400)

        options = passkey_authentication_options(
            _identity_options(request),
            allow_credentials=credentials,
        )
        challenge = await scope.challenges.create_challenge(
            user_id=str(user.id),
            kind="webauthn",
            expires_at=current_timestamp()
            + _identity_options(request).passkey_timeout_seconds,
            metadata=webauthn_challenge_metadata(
                purpose=WEBAUTHN_LOGIN_PURPOSE,
                challenge=options.challenge,
                return_to=return_to,
            ),
        )

    return JSONResponse(
        {
            "challenge_id": challenge.id,
            "publicKey": options.public_key,
        }
    )


@account_router.post(
    "/login/passkey/complete",
    include_in_schema=False,
    name="auth:passkey-login-complete",
)
async def passkey_login_complete(request: Request) -> JSONResponse:
    _ensure_passkeys_supported(request)
    payload = await _request_payload(request)
    challenge_id = _payload_text(payload, "challenge_id")
    credential = _payload_mapping(payload, "credential")
    if challenge_id is None or credential is None:
        return await _passkey_login_outcome(request, _json_error(PASSKEY_GENERIC_ERROR))

    return_to = normalise_return_to(_payload_text(payload, "return_to"))
    scope_factory = _persistence_scope_from_request(request)
    active_totp_credential_id: str | None = None
    user: User | None = None
    user_verified = False

    async with scope_factory() as scope:
        challenge = await scope.challenges.get_challenge(challenge_id)
        if (
            challenge is None
            or challenge.kind != "webauthn"
            or not challenge_has_purpose(
                challenge.metadata_payload,
                WEBAUTHN_LOGIN_PURPOSE,
            )
        ):
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR)
            )

        user = await _load_user_by_id(scope, challenge.user_id)
        if user is None or not _is_effectively_active_user(user):
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR, status_code=401)
            )

        challenge_return_to = challenge.metadata_payload.get("return_to")
        if isinstance(challenge_return_to, str) and challenge_return_to.strip():
            return_to = normalise_return_to(challenge_return_to, default=return_to)

        expected_challenge = challenge_from_metadata(challenge.metadata_payload)
        credential_id = _payload_text(credential, "id")
        if expected_challenge is None or credential_id is None:
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR), user=user
            )

        stored_credential = await scope.webauthn_credentials.get_webauthn_credential(
            credential_id
        )
        if (
            stored_credential is None
            or stored_credential.user_id != str(user.id)
            or stored_credential.status != WEBAUTHN_ACTIVE_STATUS
        ):
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR), user=user
            )

        try:
            verified = verify_passkey_authentication(
                _identity_options(request),
                credential=credential,
                expected_challenge=expected_challenge,
                stored_credential=stored_credential,
            )
        except WebAuthnCeremonyError as exc:
            await scope.challenges.consume_challenge(challenge_id)
            logger.warning(
                "Passkey login rejected: user_id=%s reason=%s",
                user.id,
                exc.reason,
            )
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR), user=user
            )

        if verified.credential_id != stored_credential.credential_id:
            return await _passkey_login_outcome(
                request, _json_error(PASSKEY_GENERIC_ERROR), user=user
            )

        await scope.webauthn_credentials.update_webauthn_authentication(
            verified.credential_id,
            sign_count=verified.sign_count,
            user_verified=verified.user_verified,
            credential_device_type=verified.credential_device_type,
            credential_backed_up=verified.credential_backed_up,
        )
        active_totp_credential_id = (
            await scope.totp_credentials.get_active_totp_credential(str(user.id))
        )
        await scope.challenges.consume_challenge(challenge_id)
        user_verified = verified.user_verified

    if user is None:
        return await _passkey_login_outcome(
            request, _json_error(PASSKEY_GENERIC_ERROR, status_code=401), user=user
        )

    options = _identity_options(request)
    if (
        options.totp_mode != TOTP_DISABLED
        and active_totp_credential_id is not None
        and (not user_verified or not options.passkey_user_verification_satisfies_totp)
    ):
        challenge_id, login_nonce = await _create_totp_challenge_after_passkey(
            request,
            user=user,
            credential_id=active_totp_credential_id,
            return_to=return_to,
        )
        response = JSONResponse(
            {
                "status": "totp_required",
                "redirect_to": _login_challenge_path(
                    request,
                    user=user,
                    challenge_id=challenge_id,
                    return_to=return_to,
                ),
            }
        )
        set_totp_login_nonce_cookie(response, request, login_nonce)
        return await _passkey_login_outcome(
            request, response, user=user, outcome="challenge_required"
        )

    ceremony_result = await complete_authentication_ceremony(
        request,
        user,
        ceremony_id=challenge_id,
        required_methods=totp_required_methods(
            options,
            has_active_totp=active_totp_credential_id is not None,
        ),
        assertions=(
            webauthn_assertion(
                str(user.id),
                ceremony_id=challenge_id,
                user_verified=user_verified,
            ),
        ),
    )
    if (
        ceremony_result.is_failure()
        and ceremony_result.error_type == ERROR_EMAIL_VERIFICATION_REQUIRED
    ):
        return await _passkey_login_outcome(
            request,
            _json_error(
                "Verify your email before signing in.",
                status_code=403,
                status="email_verification_required",
            ),
            user=user,
        )
    if (
        ceremony_result.is_failure()
        and ceremony_result.error_type == ERROR_AUTHENTICATION_METHOD_REQUIRED
    ):
        return await _passkey_login_outcome(
            request,
            _json_error(
                "Additional verification is required.",
                status_code=401,
                status="challenge_required",
            ),
            user=user,
            outcome="challenge_required",
        )
    if ceremony_result.is_failure() or ceremony_result.value is None:
        return await _passkey_login_outcome(
            request, _json_error(PASSKEY_GENERIC_ERROR), user=user
        )

    await publish_credential_access(
        request,
        operation="authenticate",
        provider="passkey",
        outcome="succeeded",
        user_id=user.id,
        email=user.email,
    )
    response = JSONResponse({"status": "ok", "redirect_to": return_to})
    set_session_cookie(response, request, ceremony_result.value, options)
    return response


async def _passkey_login_outcome(
    request: Request,
    response: JSONResponse,
    *,
    user: User | None = None,
    outcome: str = "rejected",
) -> JSONResponse:
    """Record every passkey-login terminal response without credential data."""

    await publish_credential_access(
        request,
        operation="authenticate",
        provider="passkey",
        outcome=outcome,
        user_id=user.id if user is not None else None,
        email=user.email if user is not None else None,
    )
    return response


@account_router.post(
    "/security/passkeys/revoke",
    include_in_schema=False,
    name="auth:security-passkey-revoke",
)
async def revoke_passkey(
    request: Request,
    user: User = LOGIN_REQUIRED,
) -> Response:
    _ensure_passkeys_supported(request)
    form = PasskeyRevokeCommandForm()
    await form.parse(await request_form_data(request))
    credential_row_id = command_text(form, "credential_id")
    if not credential_row_id:
        return RedirectResponse(
            url=_route_path(request, "auth:security"), status_code=303
        )

    scope_factory = _persistence_scope_from_request(request)
    error: str | None = None
    async with scope_factory() as scope:
        db_user = await _load_user_by_id(scope, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")

        credential = await scope.webauthn_credentials.get_user_webauthn_credential(
            str(db_user.id),
            credential_row_id,
        )
        if credential is None or credential.status != WEBAUTHN_ACTIVE_STATUS:
            return RedirectResponse(
                url=_route_path(request, "auth:security"),
                status_code=303,
            )

        if not await user_has_usable_account_sign_in(
            request,
            scope,
            db_user,
            exclude_passkey_id=credential.id,
        ):
            error = "Add another sign-in method before removing this passkey."
        else:
            await scope.webauthn_credentials.revoke_webauthn_credential(
                str(db_user.id),
                credential.id,
            )

    if error is not None:
        return await _security_page_response(
            request,
            user,
            form_error=error,
            status_code=400,
        )
    return RedirectResponse(url=_route_path(request, "auth:security"), status_code=303)


def _ensure_passkeys_supported(request: Request) -> None:
    if not passkeys_effectively_enabled(_identity_options(request)):
        raise HTTPException(status_code=404)


async def _request_payload(request: Request) -> Mapping[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except JSONDecodeError:
            return {}
        return payload if isinstance(payload, Mapping) else {}

    form_data = await request_form_data(request)
    return {key: value for key, value in form_data.items()}


def _payload_text(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _payload_mapping(
    payload: Mapping[str, Any],
    field_name: str,
) -> Mapping[str, Any] | None:
    value = payload.get(field_name)
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else None


def _credential_transports(credential: Mapping[str, Any]) -> tuple[str, ...]:
    response = credential.get("response")
    if not isinstance(response, Mapping):
        return ()

    transports = response.get("transports")
    if not isinstance(transports, list):
        return ()

    return tuple(transport for transport in transports if isinstance(transport, str))


def _json_error(
    message: str,
    *,
    status_code: int = 400,
    status: str = "error",
) -> JSONResponse:
    return JSONResponse(
        {"status": status, "error": message},
        status_code=status_code,
    )


async def _create_totp_challenge_after_passkey(
    request: Request,
    *,
    user: User,
    credential_id: str,
    return_to: str,
) -> tuple[str, str]:
    from .shared import _create_totp_login_challenge

    return await _create_totp_login_challenge(
        request,
        user_id=str(user.id),
        credential_id=credential_id,
    )


def _login_challenge_path(
    request: Request,
    *,
    user: User,
    challenge_id: str,
    return_to: str,
) -> str:
    query = urlencode(
        {
            "challenge_step": "totp",
            "challenge_id": challenge_id,
            "email": user.email,
            "return_to": return_to,
        }
    )
    return f"{_route_path(request, 'auth:login')}?{query}"
