from __future__ import annotations

import logging
from typing import NoReturn
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from wybra.auth.authorisation.effective import is_user_effectively_active
from wybra.auth.capabilities import login_required
from wybra.auth.email_normalisation import normalise_email_target
from wybra.auth.ids import parse_uuid
from wybra.auth.models import User
from wybra.auth.provider_credentials import (
    ProviderCredentialStorageError,
    ProviderCredentialStore,
    provider_credential_store,
)
from wybra.auth.routes.pages.login import _handle_totp_post_authentication_decision
from wybra.auth.routes.paths import normalise_return_to
from wybra.auth.sessions import resolve_current_user, session_cookie_secure_for_request
from wybra.auth.settings import identity_options_from_state
from wybra.auth.timestamps import current_timestamp
from wybra.core.exceptions import ConfigurationError
from wybra.db import DatabaseCapability
from wybra.providers.capabilities import ProvidersCapability
from wybra.providers.google import (
    GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
    GOOGLE_OAUTH_STATE_COOKIE,
    GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GOOGLE_PROVIDER_NAME,
    GoogleIDTokenClaims,
    GoogleIDTokenValidationError,
    GoogleIDTokenValidationRequest,
    GoogleIDTokenValidator,
    GoogleOAuthPurpose,
    GoogleOAuthSettings,
    GoogleOAuthState,
    GoogleOAuthTokenClient,
    GoogleOIDCIDTokenValidator,
    GoogleTokenClient,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
    GoogleTokenResponse,
    create_google_oauth_state,
    decode_google_oauth_state_cookie,
    encode_google_oauth_state_cookie,
    google_oauth_settings_from_provider,
)
from wybra.providers.policy import (
    ProviderAccountPolicy,
    ProviderAssertion,
    ProviderPolicyDecision,
    ProviderPolicyOutcome,
)
from wybra.providers.settings import ProviderSettings
from wybra.services.secrets import SecretsCapability, SecretsError
from wybra.site import get_site

google_router = APIRouter()
LOGIN_REQUIRED = Depends(login_required)
logger = logging.getLogger(__name__)


@google_router.get(
    "/login",
    include_in_schema=False,
    name="auth:google-login",
)
async def google_login_start(request: Request) -> Response:
    return _google_authorisation_redirect(
        request,
        purpose="login",
        return_to_default=_route_path(request, "auth:account"),
    )


@google_router.get(
    "/link",
    include_in_schema=False,
    name="auth:google-link",
)
async def google_link_start(request: Request, user=LOGIN_REQUIRED) -> Response:
    return _google_authorisation_redirect(
        request,
        purpose="link",
        return_to_default=_route_path(request, "auth:security"),
        user_id=str(user.id),
    )


@google_router.get(
    "/callback",
    include_in_schema=False,
    name="auth:google-callback",
)
async def google_callback(request: Request) -> Response:
    settings = _available_google_settings(request)
    state = _validated_google_callback_state(request)
    if state is None:
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google callback state is invalid.",
        )
    linking_user = await _google_linking_user(request, state)
    if state.purpose == "link" and linking_user is None:
        return _google_callback_response(
            request,
            status_code=401,
            detail="Google linking requires an active session.",
        )
    code = request.query_params.get("code")
    if not isinstance(code, str) or not code.strip():
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google callback code is missing.",
        )
    client_secret = _google_client_secret(request, settings)
    if client_secret is None:
        return _google_callback_response(
            request,
            status_code=404,
            detail="Google login is not available.",
        )
    try:
        token_response = await _google_token_client(request).exchange_code(
            GoogleTokenExchangeRequest(
                token_endpoint=settings.token_endpoint,
                client_id=settings.client_id,
                client_secret=client_secret,
                code=code.strip(),
                redirect_uri=state.redirect_uri,
            )
        )
    except GoogleTokenExchangeError:
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google token exchange failed.",
        )
    id_token = token_response.id_token
    if id_token is None or not id_token.strip():
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google ID token is invalid.",
        )
    try:
        claims = await _google_id_token_validator(request).validate(
            GoogleIDTokenValidationRequest(
                id_token=id_token,
                settings=settings,
                nonce=state.nonce,
            )
        )
    except GoogleIDTokenValidationError:
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google ID token is invalid.",
        )
    try:
        decision = await _resolve_google_account(
            request,
            state=state,
            claims=claims,
            token_response=token_response,
            linking_user=linking_user,
        )
    except ProviderCredentialStorageError:
        logger.exception("Google provider credential storage is unavailable.")
        return _google_callback_response(
            request,
            status_code=503,
            detail="Google login is not available.",
        )
    return await _google_resolution_response(request, state, decision)


async def _google_linking_user(
    request: Request,
    state: GoogleOAuthState,
) -> User | None:
    if state.purpose != "link":
        return None
    state_user_id = parse_uuid(state.user_id) if state.user_id is not None else None
    if state_user_id is None:
        return None
    user = await resolve_current_user(request)
    if user is None or parse_uuid(user.id) != state_user_id:
        return None
    return user if is_user_effectively_active(user) else None


def _google_authorisation_redirect(
    request: Request,
    *,
    purpose: GoogleOAuthPurpose,
    return_to_default: str,
    user_id: str | None = None,
) -> Response:
    settings = _available_google_settings(request)
    redirect_uri = str(request.url_for("auth:google-callback"))
    state = create_google_oauth_state(
        purpose=purpose,
        return_to=normalise_return_to(
            request.query_params.get("return_to"),
            default=return_to_default,
        ),
        redirect_uri=redirect_uri,
        user_id=user_id,
    )
    response = RedirectResponse(
        url=_google_authorisation_url(settings, state),
        status_code=303,
    )
    _set_google_oauth_state_cookie(request, response, state)
    return response


def _available_google_settings(request: Request) -> GoogleOAuthSettings:
    try:
        return google_oauth_settings_from_provider(_available_google_provider(request))
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="Google login is not available.",
        ) from exc


def _available_google_provider(request: Request) -> ProviderSettings:
    providers = get_site(request.app).optional_capability(ProvidersCapability)
    if providers is None:
        _raise_google_unavailable()
    try:
        return providers.settings.provider(GOOGLE_PROVIDER_NAME)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="Google login is not available.",
        ) from exc


def _google_authorisation_url(
    settings: GoogleOAuthSettings,
    state: GoogleOAuthState,
) -> str:
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": state.redirect_uri,
            "response_type": "code",
            "scope": " ".join(settings.scopes),
            "state": state.state,
            "nonce": state.nonce,
        }
    )
    return f"{settings.authorisation_endpoint}?{query}"


def _set_google_oauth_state_cookie(
    request: Request,
    response: Response,
    state: GoogleOAuthState,
) -> None:
    identity_options = identity_options_from_state(request.app.state)
    max_age = max(0, int(state.expires_at - current_timestamp()))
    response.set_cookie(
        GOOGLE_OAUTH_STATE_COOKIE,
        encode_google_oauth_state_cookie(
            state,
            secret=identity_options.verification_token_secret,
        ),
        max_age=max_age,
        path="/",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=identity_options.session_cookie_force_secure,
        ),
        httponly=True,
        samesite="lax",
    )


def _validated_google_callback_state(request: Request) -> GoogleOAuthState | None:
    value = request.cookies.get(GOOGLE_OAUTH_STATE_COOKIE)
    if not isinstance(value, str) or not value:
        return None
    state = decode_google_oauth_state_cookie(
        value,
        secret=_google_oauth_state_secret(request),
    )
    if state is None:
        return None
    submitted_state = request.query_params.get("state")
    if not isinstance(submitted_state, str) or submitted_state != state.state:
        return None
    if state.redirect_uri != str(request.url_for("auth:google-callback")):
        return None
    return state


def _google_client_secret(
    request: Request,
    settings: GoogleOAuthSettings,
) -> str | None:
    secrets = get_site(request.app).optional_capability(SecretsCapability)
    if secrets is None:
        return None
    source, key = settings.client_secret_reference
    try:
        return secrets.resolve(source, key).reveal()
    except SecretsError:
        return None


def _google_token_client(request: Request) -> GoogleTokenClient:
    client = getattr(
        request.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        None,
    )
    if client is None:
        return GoogleOAuthTokenClient()
    if not isinstance(client, GoogleTokenClient):
        raise TypeError("Configured Google OAuth token client is invalid.")
    return client


def _google_id_token_validator(request: Request) -> GoogleIDTokenValidator:
    validator = getattr(
        request.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        None,
    )
    if validator is None:
        return GoogleOIDCIDTokenValidator()
    if not isinstance(validator, GoogleIDTokenValidator):
        raise TypeError("Configured Google ID token validator is invalid.")
    return validator


async def _resolve_google_account(
    request: Request,
    *,
    state: GoogleOAuthState,
    claims: GoogleIDTokenClaims,
    token_response: GoogleTokenResponse,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    provider = _available_google_provider(request)
    assertion = _google_provider_assertion(claims)
    database = get_site(request.app).require_capability(DatabaseCapability)
    async with database.transaction() as session:
        store = provider_credential_store(
            session,
            getattr(request.app.state, "secret_envelope_service", None),
        )
        provider_record = await store.get_provider_by_identity(
            assertion.provider_name,
            assertion.provider_subject,
        )
        linked_user = (
            await store.get_linked_user(provider_record)
            if provider_record is not None and provider_record.provider_enabled
            else None
        )
        if state.purpose == "link":
            return await _resolve_google_linking_account(
                provider=provider,
                assertion=assertion,
                state=state,
                claims=claims,
                token_response=token_response,
                store=store,
                linked_user=linked_user,
                linking_user=linking_user,
            )

        email_match_user = await _google_email_match_user(store, claims)
        decision = ProviderAccountPolicy().evaluate_login(
            provider=provider,
            assertion=assertion,
            linked_user_id=str(linked_user.id) if linked_user is not None else None,
            linked_user_active=(
                is_user_effectively_active(linked_user)
                if linked_user is not None
                else True
            ),
            email_match_user_id=(
                str(email_match_user.id) if email_match_user is not None else None
            ),
            email_match_user_active=(
                is_user_effectively_active(email_match_user)
                if email_match_user is not None
                else True
            ),
        )
        if decision.outcome is ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED:
            if email_match_user is None:
                return _google_policy_decision(
                    ProviderPolicyOutcome.INVALID_CLAIMS,
                    assertion,
                    reason="Provider email-match user could not be resolved.",
                )
            persisted = await _persist_google_provider_link(
                store=store,
                assertion=assertion,
                claims=claims,
                token_response=token_response,
                user=email_match_user,
            )
            if not persisted:
                return _google_policy_decision(
                    ProviderPolicyOutcome.INVALID_CLAIMS,
                    assertion,
                    reason="Google token response is missing access token.",
                )
        if (
            decision.outcome is ProviderPolicyOutcome.LINKED_USER
            and linked_user is not None
        ):
            await _apply_verified_google_email(store, linked_user, claims)
        if decision.outcome is ProviderPolicyOutcome.CREATION_ALLOWED:
            return await _create_google_provider_user(
                store=store,
                assertion=assertion,
                claims=claims,
                token_response=token_response,
            )
        return decision


async def _resolve_google_linking_account(
    *,
    provider: ProviderSettings,
    assertion: ProviderAssertion,
    state: GoogleOAuthState,
    claims: GoogleIDTokenClaims,
    token_response: GoogleTokenResponse,
    store: ProviderCredentialStore,
    linked_user: User | None,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    user_id = parse_uuid(state.user_id) if state.user_id is not None else None
    if (
        user_id is None
        or linking_user is None
        or parse_uuid(linking_user.id) != user_id
    ):
        return _google_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            assertion,
            reason="Google linking state does not identify a local user.",
        )
    current_user = await store.get_user(user_id)
    if current_user is None or not is_user_effectively_active(current_user):
        return _google_policy_decision(
            ProviderPolicyOutcome.INACTIVE_USER,
            assertion,
            user_id=str(user_id),
            reason="Linking user is inactive or unavailable.",
        )
    decision = ProviderAccountPolicy().evaluate_linking(
        provider=provider,
        assertion=assertion,
        current_user_id=str(current_user.id),
        linked_user_id=str(linked_user.id) if linked_user is not None else None,
    )
    if decision.outcome is ProviderPolicyOutcome.LINK_ALLOWED:
        persisted = await _persist_google_provider_link(
            store=store,
            assertion=assertion,
            claims=claims,
            token_response=token_response,
            user=current_user,
        )
        if not persisted:
            return _google_policy_decision(
                ProviderPolicyOutcome.INVALID_CLAIMS,
                assertion,
                reason="Google token response is missing access token.",
            )
    return decision


async def _google_email_match_user(
    store: ProviderCredentialStore,
    claims: GoogleIDTokenClaims,
) -> User | None:
    normalised_email = normalise_email_target(claims.email)
    if normalised_email is None:
        return None
    return await store.get_user_by_normalised_email(normalised_email)


async def _create_google_provider_user(
    *,
    store: ProviderCredentialStore,
    assertion: ProviderAssertion,
    claims: GoogleIDTokenClaims,
    token_response: GoogleTokenResponse,
) -> ProviderPolicyDecision:
    normalised_email = normalise_email_target(claims.email)
    if normalised_email is None:
        return _google_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            assertion,
            reason="Google account email is invalid.",
        )
    created_user = await store.create_provider_user(
        email=normalised_email,
        is_verified=claims.email_verified,
    )
    persisted = await _persist_google_provider_link(
        store=store,
        assertion=assertion,
        claims=claims,
        token_response=token_response,
        user=created_user,
    )
    if not persisted:
        return _google_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            assertion,
            reason="Google token response is missing access token.",
        )
    return _google_policy_decision(
        ProviderPolicyOutcome.CREATION_ALLOWED,
        assertion,
        user_id=str(created_user.id),
    )


async def _persist_google_provider_link(
    *,
    store: ProviderCredentialStore,
    assertion: ProviderAssertion,
    claims: GoogleIDTokenClaims,
    token_response: GoogleTokenResponse,
    user: User,
) -> bool:
    access_token = token_response.access_token
    if access_token is None or not access_token.strip():
        return False
    provider = await store.upsert_provider_credential(
        provider_name=assertion.provider_name,
        provider_subject=assertion.provider_subject,
        access_token=access_token,
        refresh_token=token_response.refresh_token,
        expires_at=_google_token_expires_at(token_response),
        account_email=claims.email,
        provider_metadata={
            "email": claims.email,
            "email_verified": claims.email_verified,
        },
    )
    await store.link_provider_to_user(provider_id=provider.id, user_id=user.id)
    await _apply_verified_google_email(store, user, claims)
    return True


async def _apply_verified_google_email(
    store: ProviderCredentialStore,
    user: User,
    claims: GoogleIDTokenClaims,
) -> None:
    normalised_email = normalise_email_target(claims.email)
    if normalised_email is None:
        return
    await store.verify_matching_user_email(
        user,
        normalised_email,
        is_verified=claims.email_verified,
    )


def _google_provider_assertion(claims: GoogleIDTokenClaims) -> ProviderAssertion:
    return ProviderAssertion(
        GOOGLE_PROVIDER_NAME,
        claims.subject,
        {
            "sub": claims.subject,
            "email": claims.email,
            "email_verified": claims.email_verified,
        },
    )


def _google_token_expires_at(token_response: GoogleTokenResponse) -> float | None:
    if token_response.expires_in is None:
        return None
    return current_timestamp() + token_response.expires_in


def _google_policy_decision(
    outcome: ProviderPolicyOutcome,
    assertion: ProviderAssertion,
    *,
    user_id: str | None = None,
    reason: str | None = None,
) -> ProviderPolicyDecision:
    return ProviderPolicyDecision(
        outcome=outcome,
        provider_name=assertion.provider_name,
        provider_subject=assertion.provider_subject,
        user_id=user_id,
        reason=reason,
    )


async def _google_resolution_response(
    request: Request,
    state: GoogleOAuthState,
    decision: ProviderPolicyDecision,
) -> Response:
    if decision.outcome in {
        ProviderPolicyOutcome.CREATION_ALLOWED,
        ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED,
        ProviderPolicyOutcome.LINKED_USER,
    }:
        return await _google_login_completion_response(request, state, decision)
    if decision.outcome in {
        ProviderPolicyOutcome.ALREADY_LINKED,
        ProviderPolicyOutcome.LINK_ALLOWED,
    }:
        return _google_redirect_response(request, state.return_to)
    return _google_callback_response(
        request,
        status_code=_google_rejection_status(decision),
        detail=_google_rejection_detail(state, decision),
    )


def _google_rejection_status(decision: ProviderPolicyDecision) -> int:
    if decision.outcome is ProviderPolicyOutcome.COLLISION:
        return 409
    if decision.outcome is ProviderPolicyOutcome.DISABLED_PROVIDER:
        return 404
    if decision.outcome in {
        ProviderPolicyOutcome.CREATION_DENIED,
        ProviderPolicyOutcome.INACTIVE_USER,
    }:
        return 403
    return 400


def _google_rejection_detail(
    state: GoogleOAuthState,
    decision: ProviderPolicyDecision,
) -> str:
    if decision.outcome is ProviderPolicyOutcome.COLLISION:
        return "Google account is already linked to another user."
    if decision.outcome is ProviderPolicyOutcome.INACTIVE_USER:
        return "Google linked account is inactive."
    if decision.outcome is ProviderPolicyOutcome.DISABLED_PROVIDER:
        return "Google login is not available."
    if decision.outcome is ProviderPolicyOutcome.INVALID_CLAIMS:
        return "Google account claims are invalid."
    if decision.outcome is ProviderPolicyOutcome.CREATION_DENIED:
        return (
            "Google account linking is not allowed."
            if state.purpose == "link"
            else "Google account is not linked."
        )
    return "Google login was rejected."


async def _google_login_completion_response(
    request: Request,
    state: GoogleOAuthState,
    decision: ProviderPolicyDecision,
) -> Response:
    if decision.user_id is None:
        return _google_callback_response(
            request,
            status_code=400,
            detail="Google account claims are invalid.",
        )
    user = await _google_resolution_user(request, decision.user_id)
    if user is None:
        return _google_callback_response(
            request,
            status_code=403,
            detail="Google linked account is inactive.",
        )
    response = await _handle_totp_post_authentication_decision(
        request,
        user=user,
        email=user.email,
        return_to=state.return_to,
    )
    _clear_google_oauth_state_cookie(request, response)
    return response


async def _google_resolution_user(request: Request, user_id: str) -> User | None:
    parsed_user_id = parse_uuid(user_id)
    if parsed_user_id is None:
        return None
    database = get_site(request.app).require_capability(DatabaseCapability)
    async with database.session() as session:
        user = await session.get(User, parsed_user_id)
        return user if user is not None and is_user_effectively_active(user) else None


def _google_redirect_response(request: Request, location: str) -> Response:
    response = RedirectResponse(url=location, status_code=303)
    _clear_google_oauth_state_cookie(request, response)
    return response


def _google_callback_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
) -> Response:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    _clear_google_oauth_state_cookie(request, response)
    return response


def _clear_google_oauth_state_cookie(request: Request, response: Response) -> None:
    identity_options = identity_options_from_state(request.app.state)
    response.set_cookie(
        GOOGLE_OAUTH_STATE_COOKIE,
        "",
        max_age=0,
        path="/",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=identity_options.session_cookie_force_secure,
        ),
        httponly=True,
        samesite="lax",
    )


def _google_oauth_state_secret(request: Request) -> str:
    return identity_options_from_state(request.app.state).verification_token_secret


def _route_path(request: Request, route_name: str) -> str:
    return urlsplit(str(request.url_for(route_name))).path


def _raise_google_unavailable() -> NoReturn:
    raise HTTPException(status_code=404, detail="Google login is not available.")


module_routers = {"google": google_router}

__all__ = (
    "google_callback",
    "google_link_start",
    "google_login_start",
    "google_router",
    "module_routers",
)
