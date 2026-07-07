from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import NoReturn
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from wybra.auth.authorisation.effective import is_user_effectively_active
from wybra.auth.capabilities import login_required
from wybra.auth.ids import parse_uuid
from wybra.auth.models import User
from wybra.auth.provider_credentials import (
    ProviderCredentialStorageError,
)
from wybra.auth.routes.pages.login import _handle_totp_post_authentication_decision
from wybra.auth.routes.paths import normalise_return_to
from wybra.auth.sessions import resolve_current_user, session_cookie_secure_for_request
from wybra.auth.settings import identity_options_from_state
from wybra.auth.timestamps import current_timestamp
from wybra.core.exceptions import ConfigurationError
from wybra.db import DatabaseCapability
from wybra.providers.account_resolution import (
    ProviderAccountResolution,
    resolve_provider_account,
)
from wybra.providers.apple import (
    APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
    APPLE_OAUTH_STATE_COOKIE,
    APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    APPLE_PROVIDER_NAME,
    AppleClientSecretError,
    AppleIDTokenClaims,
    AppleIDTokenValidationError,
    AppleIDTokenValidationRequest,
    AppleIDTokenValidator,
    AppleOAuthSettings,
    AppleOAuthState,
    AppleOAuthTokenClient,
    AppleOIDCIDTokenValidator,
    AppleTokenClient,
    AppleTokenExchangeError,
    AppleTokenExchangeRequest,
    AppleTokenResponse,
    apple_oauth_settings_from_provider,
    create_apple_client_secret,
    create_apple_oauth_state,
    decode_apple_oauth_state_cookie,
    encode_apple_oauth_state_cookie,
)
from wybra.providers.capabilities import ProvidersCapability
from wybra.providers.descriptors import provider_label
from wybra.providers.flow import (
    PROVIDER_OAUTH_LINK_PURPOSE,
    PROVIDER_OAUTH_LOGIN_PURPOSE,
    ProviderOAuthPurpose,
)
from wybra.providers.github import (
    GITHUB_API_CLIENT_STATE_ATTRIBUTE,
    GITHUB_OAUTH_STATE_COOKIE,
    GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GITHUB_PROVIDER_NAME,
    GitHubAPIClient,
    GitHubAPIError,
    GitHubIdentityRequest,
    GitHubOAuthSettings,
    GitHubOAuthState,
    GitHubOAuthTokenClient,
    GitHubRESTAPIClient,
    GitHubTokenClient,
    GitHubTokenExchangeError,
    GitHubTokenExchangeRequest,
    GitHubTokenResponse,
    GitHubUserClaims,
    create_github_oauth_state,
    decode_github_oauth_state_cookie,
    encode_github_oauth_state_cookie,
    github_oauth_settings_from_provider,
    github_token_response_has_required_scopes,
)
from wybra.providers.google import (
    GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
    GOOGLE_OAUTH_STATE_COOKIE,
    GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GOOGLE_PROVIDER_NAME,
    GoogleIDTokenClaims,
    GoogleIDTokenValidationError,
    GoogleIDTokenValidationRequest,
    GoogleIDTokenValidator,
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
    ProviderAssertion,
    ProviderPolicyDecision,
    ProviderPolicyOutcome,
)
from wybra.providers.settings import ProviderSettings
from wybra.services.secrets import SecretsCapability, SecretsError
from wybra.site import get_site

google_router = APIRouter()
github_router = APIRouter()
apple_router = APIRouter()
LOGIN_REQUIRED = Depends(login_required)
logger = logging.getLogger(__name__)


type ClearOAuthStateCookie = Callable[[Request, Response], None]


@google_router.get(
    "/login",
    include_in_schema=False,
    name="auth:google-login",
)
async def google_login_start(request: Request) -> Response:
    return _google_authorisation_redirect(
        request,
        purpose=PROVIDER_OAUTH_LOGIN_PURPOSE,
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
        purpose=PROVIDER_OAUTH_LINK_PURPOSE,
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
    if state.purpose == PROVIDER_OAUTH_LINK_PURPOSE and linking_user is None:
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


@github_router.get(
    "/login",
    include_in_schema=False,
    name="auth:github-login",
)
async def github_login_start(request: Request) -> Response:
    return _github_authorisation_redirect(
        request,
        purpose=PROVIDER_OAUTH_LOGIN_PURPOSE,
        return_to_default=_route_path(request, "auth:account"),
    )


@github_router.get(
    "/link",
    include_in_schema=False,
    name="auth:github-link",
)
async def github_link_start(request: Request, user=LOGIN_REQUIRED) -> Response:
    return _github_authorisation_redirect(
        request,
        purpose=PROVIDER_OAUTH_LINK_PURPOSE,
        return_to_default=_route_path(request, "auth:security"),
        user_id=str(user.id),
    )


@github_router.get(
    "/callback",
    include_in_schema=False,
    name="auth:github-callback",
)
async def github_callback(request: Request) -> Response:
    settings = _available_github_settings(request)
    state = _validated_github_callback_state(request)
    if state is None:
        return _github_callback_response(
            request,
            status_code=400,
            detail="GitHub callback state is invalid.",
        )
    linking_user = await _github_linking_user(request, state)
    if state.purpose == PROVIDER_OAUTH_LINK_PURPOSE and linking_user is None:
        return _github_callback_response(
            request,
            status_code=401,
            detail="GitHub linking requires an active session.",
        )
    code = request.query_params.get("code")
    if not isinstance(code, str) or not code.strip():
        return _github_callback_response(
            request,
            status_code=400,
            detail="GitHub callback code is missing.",
        )
    client_secret = _github_client_secret(request, settings)
    if client_secret is None:
        return _github_callback_response(
            request,
            status_code=404,
            detail="GitHub login is not available.",
        )
    try:
        token_response = await _github_token_client(request).exchange_code(
            GitHubTokenExchangeRequest(
                token_endpoint=settings.token_endpoint,
                client_id=settings.client_id,
                client_secret=client_secret,
                code=code.strip(),
                redirect_uri=state.redirect_uri,
                code_verifier=state.code_verifier,
            )
        )
    except GitHubTokenExchangeError:
        return _github_callback_response(
            request,
            status_code=400,
            detail="GitHub token exchange failed.",
        )
    if not _valid_github_token_response(settings, token_response):
        return _github_callback_response(
            request,
            status_code=400,
            detail="GitHub token response is invalid.",
        )
    access_token = token_response.access_token
    assert access_token is not None
    try:
        claims = await _github_api_client(request).fetch_identity(
            GitHubIdentityRequest(settings=settings, access_token=access_token)
        )
    except GitHubAPIError:
        return _github_callback_response(
            request,
            status_code=400,
            detail="GitHub account claims are invalid.",
        )
    try:
        decision = await _resolve_github_account(
            request,
            state=state,
            claims=claims,
            token_response=token_response,
            linking_user=linking_user,
        )
    except ProviderCredentialStorageError:
        logger.exception("GitHub provider credential storage is unavailable.")
        return _github_callback_response(
            request,
            status_code=503,
            detail="GitHub login is not available.",
        )
    return await _github_resolution_response(request, state, decision)


@apple_router.get(
    "/login",
    include_in_schema=False,
    name="auth:apple-login",
)
async def apple_login_start(request: Request) -> Response:
    return _apple_authorisation_redirect(
        request,
        purpose=PROVIDER_OAUTH_LOGIN_PURPOSE,
        return_to_default=_route_path(request, "auth:account"),
    )


@apple_router.get(
    "/link",
    include_in_schema=False,
    name="auth:apple-link",
)
async def apple_link_start(request: Request, user=LOGIN_REQUIRED) -> Response:
    return _apple_authorisation_redirect(
        request,
        purpose=PROVIDER_OAUTH_LINK_PURPOSE,
        return_to_default=_route_path(request, "auth:security"),
        user_id=str(user.id),
    )


@apple_router.api_route(
    "/callback",
    methods=["GET", "POST"],
    include_in_schema=False,
    name="auth:apple-callback",
)
async def apple_callback(request: Request) -> Response:
    settings = _available_apple_settings(request)
    callback_params = await _apple_callback_params(request)
    state = _validated_apple_callback_state(request, callback_params)
    if state is None:
        return _apple_callback_response(
            request,
            status_code=400,
            detail="Apple callback state is invalid.",
        )
    linking_user = await _apple_linking_user(request, state)
    if state.purpose == PROVIDER_OAUTH_LINK_PURPOSE and linking_user is None:
        return _apple_callback_response(
            request,
            status_code=401,
            detail="Apple linking requires an active session.",
        )
    code = _callback_param(callback_params, "code")
    if code is None or not code.strip():
        return _apple_callback_response(
            request,
            status_code=400,
            detail="Apple callback code is missing.",
        )
    client_secret = _apple_client_secret(request, settings)
    if client_secret is None:
        return _apple_callback_response(
            request,
            status_code=404,
            detail="Apple login is not available.",
        )
    try:
        token_response = await _apple_token_client(request).exchange_code(
            AppleTokenExchangeRequest(
                token_endpoint=settings.token_endpoint,
                client_id=settings.client_id,
                client_secret=client_secret,
                code=code.strip(),
                redirect_uri=state.redirect_uri,
            )
        )
    except AppleTokenExchangeError:
        return _apple_callback_response(
            request,
            status_code=400,
            detail="Apple token exchange failed.",
        )
    if not _valid_apple_token_response(token_response):
        return _apple_callback_response(
            request,
            status_code=400,
            detail="Apple token response is invalid.",
        )
    id_token = token_response.id_token
    assert id_token is not None
    try:
        claims = await _apple_id_token_validator(request).validate(
            AppleIDTokenValidationRequest(
                id_token=id_token,
                settings=settings,
                nonce=state.nonce,
            )
        )
    except AppleIDTokenValidationError:
        return _apple_callback_response(
            request,
            status_code=400,
            detail="Apple ID token is invalid.",
        )
    try:
        decision = await _resolve_apple_account(
            request,
            state=state,
            claims=claims,
            token_response=token_response,
            linking_user=linking_user,
        )
    except ProviderCredentialStorageError:
        logger.exception("Apple provider credential storage is unavailable.")
        return _apple_callback_response(
            request,
            status_code=503,
            detail="Apple login is not available.",
        )
    return await _apple_resolution_response(request, state, decision)


async def _google_linking_user(
    request: Request,
    state: GoogleOAuthState,
) -> User | None:
    return await _provider_linking_user(
        request,
        purpose=state.purpose,
        user_id=state.user_id,
    )


def _google_authorisation_redirect(
    request: Request,
    *,
    purpose: ProviderOAuthPurpose,
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
    _set_oauth_state_cookie(
        request,
        response,
        cookie_name=GOOGLE_OAUTH_STATE_COOKIE,
        cookie_value=encode_google_oauth_state_cookie(
            state,
            secret=_oauth_state_secret(request),
        ),
        expires_at=state.expires_at,
    )


def _validated_google_callback_state(request: Request) -> GoogleOAuthState | None:
    value = request.cookies.get(GOOGLE_OAUTH_STATE_COOKIE)
    if not isinstance(value, str) or not value:
        return None
    state = decode_google_oauth_state_cookie(
        value,
        secret=_oauth_state_secret(request),
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
        _raise_invalid_configured_client_type("Google OAuth token client", client)
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
        _raise_invalid_configured_client_type("Google ID token validator", validator)
    return validator


async def _provider_resolution_response(
    request: Request,
    *,
    return_to: str,
    purpose: ProviderOAuthPurpose,
    decision: ProviderPolicyDecision,
    provider_label: str,
    clear_state_cookie: ClearOAuthStateCookie,
) -> Response:
    if decision.outcome in {
        ProviderPolicyOutcome.CREATION_ALLOWED,
        ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED,
        ProviderPolicyOutcome.LINKED_USER,
    }:
        return await _provider_login_completion_response(
            request,
            return_to=return_to,
            decision=decision,
            provider_label=provider_label,
            clear_state_cookie=clear_state_cookie,
        )
    if decision.outcome in {
        ProviderPolicyOutcome.ALREADY_LINKED,
        ProviderPolicyOutcome.LINK_ALLOWED,
    }:
        return _provider_redirect_response(
            request,
            return_to,
            clear_state_cookie=clear_state_cookie,
        )
    return _provider_callback_response(
        request,
        status_code=_provider_rejection_status(decision),
        detail=_provider_rejection_detail(
            purpose=purpose,
            decision=decision,
            provider_label=provider_label,
        ),
        clear_state_cookie=clear_state_cookie,
    )


def _provider_rejection_status(decision: ProviderPolicyDecision) -> int:
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


def _provider_rejection_detail(
    *,
    purpose: ProviderOAuthPurpose,
    decision: ProviderPolicyDecision,
    provider_label: str,
) -> str:
    if decision.outcome is ProviderPolicyOutcome.COLLISION:
        return f"{provider_label} account is already linked to another user."
    if decision.outcome is ProviderPolicyOutcome.INACTIVE_USER:
        return f"{provider_label} linked account is inactive."
    if decision.outcome is ProviderPolicyOutcome.DISABLED_PROVIDER:
        return f"{provider_label} login is not available."
    if decision.outcome is ProviderPolicyOutcome.INVALID_CLAIMS:
        return f"{provider_label} account claims are invalid."
    if decision.outcome is ProviderPolicyOutcome.CREATION_DENIED:
        return (
            f"{provider_label} account linking is not allowed."
            if purpose == PROVIDER_OAUTH_LINK_PURPOSE
            else f"{provider_label} account is not linked."
        )
    return f"{provider_label} login was rejected."


async def _provider_login_completion_response(
    request: Request,
    *,
    return_to: str,
    decision: ProviderPolicyDecision,
    provider_label: str,
    clear_state_cookie: ClearOAuthStateCookie,
) -> Response:
    if decision.user_id is None:
        return _provider_callback_response(
            request,
            status_code=400,
            detail=f"{provider_label} account claims are invalid.",
            clear_state_cookie=clear_state_cookie,
        )
    user = await _provider_resolution_user(request, decision.user_id)
    if user is None:
        return _provider_callback_response(
            request,
            status_code=403,
            detail=f"{provider_label} linked account is inactive.",
            clear_state_cookie=clear_state_cookie,
        )
    response = await _handle_totp_post_authentication_decision(
        request,
        user=user,
        email=user.email,
        return_to=return_to,
    )
    clear_state_cookie(request, response)
    return response


async def _provider_resolution_user(request: Request, user_id: str) -> User | None:
    parsed_user_id = parse_uuid(user_id)
    if parsed_user_id is None:
        return None
    database = get_site(request.app).require_capability(DatabaseCapability)
    async with database.transaction() as session:
        user = await User.get_or_none(id=parsed_user_id, using_db=session)
        return user if user is not None and is_user_effectively_active(user) else None


def _provider_redirect_response(
    request: Request,
    location: str,
    *,
    clear_state_cookie: ClearOAuthStateCookie,
) -> Response:
    response = RedirectResponse(url=location, status_code=303)
    clear_state_cookie(request, response)
    return response


def _provider_callback_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
    clear_state_cookie: ClearOAuthStateCookie,
) -> Response:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    clear_state_cookie(request, response)
    return response


async def _provider_linking_user(
    request: Request,
    *,
    purpose: ProviderOAuthPurpose,
    user_id: str | None,
) -> User | None:
    if purpose != PROVIDER_OAUTH_LINK_PURPOSE:
        return None
    state_user_id = parse_uuid(user_id) if user_id is not None else None
    if state_user_id is None:
        return None
    user = await resolve_current_user(request)
    if user is None or parse_uuid(user.id) != state_user_id:
        return None
    return user if is_user_effectively_active(user) else None


def _set_oauth_state_cookie(
    request: Request,
    response: Response,
    *,
    cookie_name: str,
    cookie_value: str,
    expires_at: float,
) -> None:
    identity_options = identity_options_from_state(request.app.state)
    max_age = max(0, int(expires_at - current_timestamp()))
    response.set_cookie(
        cookie_name,
        cookie_value,
        max_age=max_age,
        path="/",
        secure=session_cookie_secure_for_request(
            request,
            force_secure=identity_options.session_cookie_force_secure,
        ),
        httponly=True,
        samesite="lax",
    )


def _clear_oauth_state_cookie(
    request: Request,
    response: Response,
    *,
    cookie_name: str,
) -> None:
    identity_options = identity_options_from_state(request.app.state)
    response.set_cookie(
        cookie_name,
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
    return await resolve_provider_account(
        request,
        resolution=ProviderAccountResolution(
            provider=provider,
            assertion=assertion,
            purpose=state.purpose,
            state_user_id=state.user_id,
            provider_label=provider_label(GOOGLE_PROVIDER_NAME),
            account_email=claims.email,
            email_verified=claims.email_verified,
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            expires_in=token_response.expires_in,
            provider_metadata={
                "email": claims.email,
                "email_verified": claims.email_verified,
            },
        ),
        linking_user=linking_user,
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


async def _google_resolution_response(
    request: Request,
    state: GoogleOAuthState,
    decision: ProviderPolicyDecision,
) -> Response:
    return await _provider_resolution_response(
        request,
        return_to=state.return_to,
        purpose=state.purpose,
        decision=decision,
        provider_label=provider_label(GOOGLE_PROVIDER_NAME),
        clear_state_cookie=_clear_google_oauth_state_cookie,
    )


def _google_callback_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
) -> Response:
    return _provider_callback_response(
        request,
        status_code=status_code,
        detail=detail,
        clear_state_cookie=_clear_google_oauth_state_cookie,
    )


def _clear_google_oauth_state_cookie(request: Request, response: Response) -> None:
    _clear_oauth_state_cookie(
        request,
        response,
        cookie_name=GOOGLE_OAUTH_STATE_COOKIE,
    )


async def _github_linking_user(
    request: Request,
    state: GitHubOAuthState,
) -> User | None:
    return await _provider_linking_user(
        request,
        purpose=state.purpose,
        user_id=state.user_id,
    )


def _github_authorisation_redirect(
    request: Request,
    *,
    purpose: ProviderOAuthPurpose,
    return_to_default: str,
    user_id: str | None = None,
) -> Response:
    settings = _available_github_settings(request)
    redirect_uri = str(request.url_for("auth:github-callback"))
    state = create_github_oauth_state(
        purpose=purpose,
        return_to=normalise_return_to(
            request.query_params.get("return_to"),
            default=return_to_default,
        ),
        redirect_uri=redirect_uri,
        user_id=user_id,
    )
    response = RedirectResponse(
        url=_github_authorisation_url(settings, state),
        status_code=303,
    )
    _set_github_oauth_state_cookie(request, response, state)
    return response


def _available_github_settings(request: Request) -> GitHubOAuthSettings:
    try:
        return github_oauth_settings_from_provider(_available_github_provider(request))
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="GitHub login is not available.",
        ) from exc


def _available_github_provider(request: Request) -> ProviderSettings:
    providers = get_site(request.app).optional_capability(ProvidersCapability)
    if providers is None:
        _raise_github_unavailable()
    try:
        return providers.settings.provider(GITHUB_PROVIDER_NAME)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="GitHub login is not available.",
        ) from exc


def _github_authorisation_url(
    settings: GitHubOAuthSettings,
    state: GitHubOAuthState,
) -> str:
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": state.redirect_uri,
            "scope": " ".join(settings.scopes),
            "state": state.state,
            "code_challenge": state.code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.authorisation_endpoint}?{query}"


def _set_github_oauth_state_cookie(
    request: Request,
    response: Response,
    state: GitHubOAuthState,
) -> None:
    _set_oauth_state_cookie(
        request,
        response,
        cookie_name=GITHUB_OAUTH_STATE_COOKIE,
        cookie_value=encode_github_oauth_state_cookie(
            state,
            secret=_oauth_state_secret(request),
        ),
        expires_at=state.expires_at,
    )


def _validated_github_callback_state(request: Request) -> GitHubOAuthState | None:
    value = request.cookies.get(GITHUB_OAUTH_STATE_COOKIE)
    if not isinstance(value, str) or not value:
        return None
    state = decode_github_oauth_state_cookie(
        value,
        secret=_oauth_state_secret(request),
    )
    if state is None:
        return None
    submitted_state = request.query_params.get("state")
    if not isinstance(submitted_state, str) or submitted_state != state.state:
        return None
    if state.redirect_uri != str(request.url_for("auth:github-callback")):
        return None
    return state


def _github_client_secret(
    request: Request,
    settings: GitHubOAuthSettings,
) -> str | None:
    secrets = get_site(request.app).optional_capability(SecretsCapability)
    if secrets is None:
        return None
    source, key = settings.client_secret_reference
    try:
        return secrets.resolve(source, key).reveal()
    except SecretsError:
        return None


def _github_token_client(request: Request) -> GitHubTokenClient:
    client = getattr(
        request.app.state,
        GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        None,
    )
    if client is None:
        return GitHubOAuthTokenClient()
    if not isinstance(client, GitHubTokenClient):
        _raise_invalid_configured_client_type("GitHub OAuth token client", client)
    return client


def _github_api_client(request: Request) -> GitHubAPIClient:
    client = getattr(
        request.app.state,
        GITHUB_API_CLIENT_STATE_ATTRIBUTE,
        None,
    )
    if client is None:
        return GitHubRESTAPIClient()
    if not isinstance(client, GitHubAPIClient):
        _raise_invalid_configured_client_type("GitHub API client", client)
    return client


def _valid_github_token_response(
    settings: GitHubOAuthSettings,
    token_response: GitHubTokenResponse,
) -> bool:
    access_token = token_response.access_token
    if access_token is None or not access_token.strip():
        return False
    token_type = token_response.token_type
    if token_type is None or token_type.lower() != "bearer":
        return False
    return github_token_response_has_required_scopes(
        token_response,
        settings.scopes,
    )


async def _resolve_github_account(
    request: Request,
    *,
    state: GitHubOAuthState,
    claims: GitHubUserClaims,
    token_response: GitHubTokenResponse,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    provider = _available_github_provider(request)
    assertion = _github_provider_assertion(claims)
    return await resolve_provider_account(
        request,
        resolution=ProviderAccountResolution(
            provider=provider,
            assertion=assertion,
            purpose=state.purpose,
            state_user_id=state.user_id,
            provider_label=provider_label(GITHUB_PROVIDER_NAME),
            account_email=claims.email,
            email_verified=claims.email_verified,
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            expires_in=token_response.expires_in,
            provider_metadata=dict(claims.claims),
        ),
        linking_user=linking_user,
    )


def _github_provider_assertion(claims: GitHubUserClaims) -> ProviderAssertion:
    return ProviderAssertion(
        GITHUB_PROVIDER_NAME,
        claims.subject,
        {
            "id": claims.subject,
            "sub": claims.subject,
            "email": claims.email,
            "email_verified": claims.email_verified,
            "login": claims.login,
        },
    )


async def _github_resolution_response(
    request: Request,
    state: GitHubOAuthState,
    decision: ProviderPolicyDecision,
) -> Response:
    return await _provider_resolution_response(
        request,
        return_to=state.return_to,
        purpose=state.purpose,
        decision=decision,
        provider_label=provider_label(GITHUB_PROVIDER_NAME),
        clear_state_cookie=_clear_github_oauth_state_cookie,
    )


def _github_callback_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
) -> Response:
    return _provider_callback_response(
        request,
        status_code=status_code,
        detail=detail,
        clear_state_cookie=_clear_github_oauth_state_cookie,
    )


def _clear_github_oauth_state_cookie(request: Request, response: Response) -> None:
    _clear_oauth_state_cookie(
        request,
        response,
        cookie_name=GITHUB_OAUTH_STATE_COOKIE,
    )


async def _apple_callback_params(request: Request) -> FormData | Mapping[str, object]:
    if request.method.upper() == "POST":
        return await request.form()
    return request.query_params


def _callback_param(
    params: FormData | Mapping[str, object],
    name: str,
) -> str | None:
    value = params.get(name)
    return value if isinstance(value, str) else None


async def _apple_linking_user(
    request: Request,
    state: AppleOAuthState,
) -> User | None:
    return await _provider_linking_user(
        request,
        purpose=state.purpose,
        user_id=state.user_id,
    )


def _apple_authorisation_redirect(
    request: Request,
    *,
    purpose: ProviderOAuthPurpose,
    return_to_default: str,
    user_id: str | None = None,
) -> Response:
    settings = _available_apple_settings(request)
    redirect_uri = str(request.url_for("auth:apple-callback"))
    state = create_apple_oauth_state(
        purpose=purpose,
        return_to=normalise_return_to(
            request.query_params.get("return_to"),
            default=return_to_default,
        ),
        redirect_uri=redirect_uri,
        user_id=user_id,
    )
    response = RedirectResponse(
        url=_apple_authorisation_url(settings, state),
        status_code=303,
    )
    _set_apple_oauth_state_cookie(request, response, state)
    return response


def _available_apple_settings(request: Request) -> AppleOAuthSettings:
    try:
        return apple_oauth_settings_from_provider(_available_apple_provider(request))
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="Apple login is not available.",
        ) from exc


def _available_apple_provider(request: Request) -> ProviderSettings:
    providers = get_site(request.app).optional_capability(ProvidersCapability)
    if providers is None:
        _raise_apple_unavailable()
    try:
        return providers.settings.provider(APPLE_PROVIDER_NAME)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=404,
            detail="Apple login is not available.",
        ) from exc


def _apple_authorisation_url(
    settings: AppleOAuthSettings,
    state: AppleOAuthState,
) -> str:
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": state.redirect_uri,
            "response_type": "code",
            "response_mode": "form_post",
            "scope": " ".join(settings.scopes),
            "state": state.state,
            "nonce": state.nonce,
        }
    )
    return f"{settings.authorisation_endpoint}?{query}"


def _set_apple_oauth_state_cookie(
    request: Request,
    response: Response,
    state: AppleOAuthState,
) -> None:
    _set_oauth_state_cookie(
        request,
        response,
        cookie_name=APPLE_OAUTH_STATE_COOKIE,
        cookie_value=encode_apple_oauth_state_cookie(
            state,
            secret=_oauth_state_secret(request),
        ),
        expires_at=state.expires_at,
    )


def _validated_apple_callback_state(
    request: Request,
    params: FormData | Mapping[str, object],
) -> AppleOAuthState | None:
    value = request.cookies.get(APPLE_OAUTH_STATE_COOKIE)
    if not isinstance(value, str) or not value:
        return None
    state = decode_apple_oauth_state_cookie(
        value,
        secret=_oauth_state_secret(request),
    )
    if state is None:
        return None
    submitted_state = _callback_param(params, "state")
    if not isinstance(submitted_state, str) or submitted_state != state.state:
        return None
    if state.redirect_uri != str(request.url_for("auth:apple-callback")):
        return None
    return state


def _apple_client_secret(
    request: Request,
    settings: AppleOAuthSettings,
) -> str | None:
    private_key = _apple_private_key(request, settings)
    if private_key is None:
        return None
    try:
        return create_apple_client_secret(settings, private_key=private_key)
    except AppleClientSecretError as exc:
        logger.warning("Apple client secret generation failed.", exc_info=exc)
        return None


def _apple_private_key(
    request: Request,
    settings: AppleOAuthSettings,
) -> str | None:
    secrets = get_site(request.app).optional_capability(SecretsCapability)
    if secrets is None:
        return None
    source, key = settings.private_key_reference
    try:
        return secrets.resolve(source, key).reveal()
    except SecretsError as exc:
        logger.warning(
            "Apple private key resolution failed: source=%s key=%s",
            source,
            key,
            exc_info=exc,
        )
        return None


def _apple_token_client(request: Request) -> AppleTokenClient:
    client = getattr(
        request.app.state,
        APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        None,
    )
    if client is None:
        return AppleOAuthTokenClient()
    if not isinstance(client, AppleTokenClient):
        _raise_invalid_configured_client_type("Apple OAuth token client", client)
    return client


def _apple_id_token_validator(request: Request) -> AppleIDTokenValidator:
    validator = getattr(
        request.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        None,
    )
    if validator is None:
        return AppleOIDCIDTokenValidator()
    if not isinstance(validator, AppleIDTokenValidator):
        _raise_invalid_configured_client_type("Apple ID token validator", validator)
    return validator


def _valid_apple_token_response(token_response: AppleTokenResponse) -> bool:
    access_token = token_response.access_token
    if access_token is None or not access_token.strip():
        return False
    id_token = token_response.id_token
    if id_token is None or not id_token.strip():
        return False
    token_type = token_response.token_type
    return token_type is not None and token_type.lower() == "bearer"


async def _resolve_apple_account(
    request: Request,
    *,
    state: AppleOAuthState,
    claims: AppleIDTokenClaims,
    token_response: AppleTokenResponse,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    provider = _available_apple_provider(request)
    assertion = _apple_provider_assertion(claims)
    return await resolve_provider_account(
        request,
        resolution=ProviderAccountResolution(
            provider=provider,
            assertion=assertion,
            purpose=state.purpose,
            state_user_id=state.user_id,
            provider_label=provider_label(APPLE_PROVIDER_NAME),
            account_email=claims.email,
            email_verified=claims.email_verified,
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            expires_in=token_response.expires_in,
            provider_metadata=dict(claims.claims),
        ),
        linking_user=linking_user,
    )


def _apple_provider_assertion(claims: AppleIDTokenClaims) -> ProviderAssertion:
    return ProviderAssertion(
        APPLE_PROVIDER_NAME,
        claims.subject,
        {
            "sub": claims.subject,
            "email": claims.email,
            "email_verified": claims.email_verified,
        },
    )


async def _apple_resolution_response(
    request: Request,
    state: AppleOAuthState,
    decision: ProviderPolicyDecision,
) -> Response:
    return await _provider_resolution_response(
        request,
        return_to=state.return_to,
        purpose=state.purpose,
        decision=decision,
        provider_label=provider_label(APPLE_PROVIDER_NAME),
        clear_state_cookie=_clear_apple_oauth_state_cookie,
    )


def _apple_callback_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
) -> Response:
    return _provider_callback_response(
        request,
        status_code=status_code,
        detail=detail,
        clear_state_cookie=_clear_apple_oauth_state_cookie,
    )


def _clear_apple_oauth_state_cookie(request: Request, response: Response) -> None:
    _clear_oauth_state_cookie(
        request,
        response,
        cookie_name=APPLE_OAUTH_STATE_COOKIE,
    )


def _oauth_state_secret(request: Request) -> str:
    return identity_options_from_state(request.app.state).verification_token_secret


def _raise_invalid_configured_client_type(name: str, value: object) -> NoReturn:
    raise TypeError(
        f"Configured {name} is invalid: actual_type={type(value).__name__}."
    )


def _route_path(request: Request, route_name: str) -> str:
    return urlsplit(str(request.url_for(route_name))).path


def _raise_google_unavailable() -> NoReturn:
    raise HTTPException(status_code=404, detail="Google login is not available.")


def _raise_github_unavailable() -> NoReturn:
    raise HTTPException(status_code=404, detail="GitHub login is not available.")


def _raise_apple_unavailable() -> NoReturn:
    raise HTTPException(status_code=404, detail="Apple login is not available.")


module_routers = {
    APPLE_PROVIDER_NAME: apple_router,
    GOOGLE_PROVIDER_NAME: google_router,
    GITHUB_PROVIDER_NAME: github_router,
}

__all__ = (
    "apple_callback",
    "apple_link_start",
    "apple_login_start",
    "apple_router",
    "github_callback",
    "github_link_start",
    "github_login_start",
    "github_router",
    "google_callback",
    "google_link_start",
    "google_login_start",
    "google_router",
    "module_routers",
)
