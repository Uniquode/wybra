from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from secrets import token_urlsafe
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable
from urllib.parse import urlencode

from jwt import PyJWKClient

from wybra.auth.timestamps import current_timestamp
from wybra.core.exceptions import ConfigurationError
from wybra.providers.http import https_endpoint, https_request, json_object_response
from wybra.providers.oauth_state import (
    decode_signed_oauth_state,
    encode_signed_oauth_state,
)
from wybra.providers.oidc import oidc_id_token_payload
from wybra.providers.settings import (
    PROVIDER_CLIENT_ID_FIELD,
    PROVIDER_CLIENT_SECRET_KEY_FIELD,
    PROVIDER_SECRETS_FIELD,
    ProviderSettings,
)
from wybra.services.secrets import SecretSource

GOOGLE_PROVIDER_NAME: Final = "google"
GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE: Final = "google_id_token_validator"
GOOGLE_OAUTH_STATE_COOKIE: Final = "wybra_google_oauth_state"
GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE: Final = "google_oauth_token_client"
GOOGLE_OAUTH_STATE_EXPIRY_SECONDS: Final[float] = 300.0
GOOGLE_OAUTH_STATE_BYTES: Final = 32
GOOGLE_OAUTH_NONCE_BYTES: Final = 32
GOOGLE_DEFAULT_SCOPES: Final = ("openid", "email", "profile")
GOOGLE_DEFAULT_ISSUER: Final = "https://accounts.google.com"
GOOGLE_DEFAULT_AUTHORISATION_ENDPOINT: Final = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)
GOOGLE_DEFAULT_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
GOOGLE_DEFAULT_JWKS_URI: Final = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_DEFAULT_DISCOVERY_DOCUMENT_URL: Final = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
GoogleOAuthPurpose = Literal["login", "link"]
GoogleJwksClientFactory = Callable[[str], "GoogleJwksClient"]


@dataclass(frozen=True, slots=True)
class GoogleOAuthSettings:
    provider_name: str
    client_id: str
    client_secret_reference: tuple[SecretSource, str]
    scopes: tuple[str, ...] = GOOGLE_DEFAULT_SCOPES
    issuer: str = GOOGLE_DEFAULT_ISSUER
    authorisation_endpoint: str = GOOGLE_DEFAULT_AUTHORISATION_ENDPOINT
    token_endpoint: str = GOOGLE_DEFAULT_TOKEN_ENDPOINT
    jwks_uri: str = GOOGLE_DEFAULT_JWKS_URI
    discovery_document_url: str = GOOGLE_DEFAULT_DISCOVERY_DOCUMENT_URL


@dataclass(frozen=True, slots=True)
class GoogleTokenExchangeRequest:
    token_endpoint: str
    client_id: str
    client_secret: str = field(repr=False)
    code: str = field(repr=False)
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class GoogleTokenResponse:
    access_token: str | None = None
    id_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    scope: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict, repr=False)


class GoogleTokenExchangeError(RuntimeError):
    """Raised when Google authorisation-code token exchange fails."""


class GoogleIDTokenValidationError(RuntimeError):
    """Raised when a Google ID token cannot be trusted."""


class GoogleJwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@runtime_checkable
class GoogleTokenClient(Protocol):
    async def exchange_code(
        self,
        request: GoogleTokenExchangeRequest,
    ) -> GoogleTokenResponse: ...


@dataclass(frozen=True, slots=True)
class GoogleOAuthTokenClient:
    timeout: float = 10.0

    async def exchange_code(
        self,
        request: GoogleTokenExchangeRequest,
    ) -> GoogleTokenResponse:
        return await asyncio.to_thread(
            _exchange_google_authorisation_code,
            request,
            self.timeout,
        )


@dataclass(frozen=True, slots=True)
class GoogleIDTokenValidationRequest:
    id_token: str = field(repr=False)
    settings: GoogleOAuthSettings
    nonce: str


@dataclass(frozen=True, slots=True)
class GoogleIDTokenClaims:
    subject: str
    email: str
    email_verified: bool
    nonce: str
    claims: Mapping[str, object] = field(default_factory=dict, repr=False)


@runtime_checkable
class GoogleIDTokenValidator(Protocol):
    async def validate(
        self,
        request: GoogleIDTokenValidationRequest,
    ) -> GoogleIDTokenClaims: ...


@dataclass(frozen=True, slots=True)
class GoogleOIDCIDTokenValidator:
    jwks_client_factory: GoogleJwksClientFactory = PyJWKClient

    async def validate(
        self,
        request: GoogleIDTokenValidationRequest,
    ) -> GoogleIDTokenClaims:
        return await asyncio.to_thread(
            _validate_google_id_token,
            request,
            self.jwks_client_factory,
        )


@dataclass(frozen=True, slots=True)
class GoogleOAuthState:
    provider_name: str
    purpose: GoogleOAuthPurpose
    state: str
    nonce: str
    return_to: str
    redirect_uri: str
    expires_at: float
    user_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_name != GOOGLE_PROVIDER_NAME:
            raise ValueError(
                f"Google OAuth state requires provider {GOOGLE_PROVIDER_NAME!r}."
            )
        if self.purpose not in ("login", "link"):
            raise ValueError("Google OAuth state purpose must be login or link.")
        for field_name in ("state", "nonce", "return_to", "redirect_uri"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Google OAuth state {field_name} must be non-blank.")
        if self.expires_at <= 0:
            raise ValueError("Google OAuth state expiry must be positive.")
        if self.user_id is not None and not self.user_id.strip():
            raise ValueError("Google OAuth state user_id must be non-blank.")


def google_oauth_settings_from_provider(
    provider: ProviderSettings,
) -> GoogleOAuthSettings:
    if provider.name != GOOGLE_PROVIDER_NAME:
        raise ConfigurationError(
            f"Google OAuth settings require provider {GOOGLE_PROVIDER_NAME!r}."
        )
    if provider.client_id is None:
        raise ConfigurationError(
            f"Google provider must configure {PROVIDER_CLIENT_ID_FIELD!r}."
        )
    client_secret_reference = provider.required_client_secret_reference()
    if client_secret_reference is None:
        raise ConfigurationError(
            "Google provider must configure both "
            f"{PROVIDER_SECRETS_FIELD!r} and "
            f"{PROVIDER_CLIENT_SECRET_KEY_FIELD!r}."
        )
    return GoogleOAuthSettings(
        provider_name=provider.name,
        client_id=provider.client_id,
        client_secret_reference=client_secret_reference,
    )


def create_google_oauth_state(
    *,
    purpose: GoogleOAuthPurpose,
    return_to: str,
    redirect_uri: str,
    user_id: str | None = None,
    now: float | None = None,
) -> GoogleOAuthState:
    return GoogleOAuthState(
        provider_name=GOOGLE_PROVIDER_NAME,
        purpose=purpose,
        state=token_urlsafe(GOOGLE_OAUTH_STATE_BYTES),
        nonce=token_urlsafe(GOOGLE_OAUTH_NONCE_BYTES),
        return_to=return_to,
        redirect_uri=redirect_uri,
        expires_at=(current_timestamp() if now is None else now)
        + GOOGLE_OAUTH_STATE_EXPIRY_SECONDS,
        user_id=user_id,
    )


def google_token_response_from_payload(
    payload: Mapping[str, object],
) -> GoogleTokenResponse:
    return GoogleTokenResponse(
        access_token=_optional_payload_str(payload, "access_token"),
        id_token=_optional_payload_str(payload, "id_token"),
        refresh_token=_optional_payload_str(payload, "refresh_token"),
        token_type=_optional_payload_str(payload, "token_type"),
        expires_in=_optional_payload_int(payload, "expires_in"),
        scope=_optional_payload_str(payload, "scope"),
        raw=dict(payload),
    )


def google_id_token_claims_from_payload(
    payload: Mapping[str, object],
    *,
    expected_nonce: str,
) -> GoogleIDTokenClaims:
    subject = _required_payload_str(payload, "sub")
    email = _required_payload_str(payload, "email")
    email_verified = _required_payload_bool(payload, "email_verified")
    nonce = _required_payload_str(payload, "nonce")
    if not hmac.compare_digest(nonce, expected_nonce):
        raise GoogleIDTokenValidationError("Google ID token nonce is invalid.")
    return GoogleIDTokenClaims(
        subject=subject,
        email=email,
        email_verified=email_verified,
        nonce=nonce,
        claims=dict(payload),
    )


def encode_google_oauth_state_cookie(
    state: GoogleOAuthState,
    *,
    secret: str,
) -> str:
    return encode_signed_oauth_state(asdict(state), secret=secret)


def decode_google_oauth_state_cookie(
    value: str,
    *,
    secret: str,
    now: float | None = None,
) -> GoogleOAuthState | None:
    return decode_signed_oauth_state(
        value,
        secret=secret,
        state_factory=_google_oauth_state_from_payload,
        now=now,
    )


def _google_oauth_state_from_payload(
    payload: dict[object, object],
) -> GoogleOAuthState | None:
    try:
        provider_name = payload["provider_name"]
        purpose = payload["purpose"]
        state = payload["state"]
        nonce = payload["nonce"]
        return_to = payload["return_to"]
        redirect_uri = payload["redirect_uri"]
        expires_at = payload["expires_at"]
        user_id = payload.get("user_id")
    except KeyError:
        return None
    if not (
        isinstance(provider_name, str)
        and purpose in ("login", "link")
        and isinstance(state, str)
        and isinstance(nonce, str)
        and isinstance(return_to, str)
        and isinstance(redirect_uri, str)
        and isinstance(expires_at, (int, float))
        and (user_id is None or isinstance(user_id, str))
    ):
        return None
    try:
        return GoogleOAuthState(
            provider_name=provider_name,
            purpose=cast(GoogleOAuthPurpose, purpose),
            state=state,
            nonce=nonce,
            return_to=return_to,
            redirect_uri=redirect_uri,
            expires_at=float(expires_at),
            user_id=user_id,
        )
    except ValueError:
        return None


def _exchange_google_authorisation_code(
    request: GoogleTokenExchangeRequest,
    timeout: float,
) -> GoogleTokenResponse:
    parsed_endpoint = https_endpoint(
        request.token_endpoint,
        error_type=GoogleTokenExchangeError,
        error_message="Google token endpoint must be HTTPS.",
    )
    body = urlencode(
        {
            "client_id": request.client_id,
            "client_secret": request.client_secret,
            "code": request.code,
            "grant_type": "authorization_code",
            "redirect_uri": request.redirect_uri,
        }
    ).encode("ascii")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    response_body = https_request(
        parsed_endpoint,
        method="POST",
        body=body,
        headers=headers,
        timeout=timeout,
        error_type=GoogleTokenExchangeError,
        error_message="Google token exchange failed.",
    )
    payload = json_object_response(
        response_body,
        error_type=GoogleTokenExchangeError,
        invalid_json_message="Google token exchange returned invalid JSON.",
        invalid_payload_message="Google token exchange returned an invalid response.",
    )
    return google_token_response_from_payload(payload)


def _validate_google_id_token(
    request: GoogleIDTokenValidationRequest,
    jwks_client_factory: GoogleJwksClientFactory,
) -> GoogleIDTokenClaims:
    payload = oidc_id_token_payload(
        request.id_token,
        jwks_uri=request.settings.jwks_uri,
        audience=request.settings.client_id,
        issuer=request.settings.issuer,
        jwks_client_factory=jwks_client_factory,
        error_type=GoogleIDTokenValidationError,
        missing_message="Google ID token is missing.",
        invalid_message="Google ID token is invalid.",
        invalid_payload_message="Google ID token payload is invalid.",
    )
    return google_id_token_claims_from_payload(
        payload,
        expected_nonce=request.nonce,
    )


def _optional_payload_str(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise GoogleTokenExchangeError(
        f"Google token exchange response field {field_name!r} must be a string."
    )


def _optional_payload_int(
    payload: Mapping[str, object],
    field_name: str,
) -> int | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise GoogleTokenExchangeError(
        f"Google token exchange response field {field_name!r} must be an integer."
    )


def _required_payload_str(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise GoogleIDTokenValidationError(
        f"Google ID token claim {field_name!r} must be a non-blank string."
    )


def _required_payload_bool(
    payload: Mapping[str, object],
    field_name: str,
) -> bool:
    value = payload.get(field_name)
    if isinstance(value, bool):
        return value
    raise GoogleIDTokenValidationError(
        f"Google ID token claim {field_name!r} must be a boolean."
    )


__all__ = (
    "GOOGLE_DEFAULT_AUTHORISATION_ENDPOINT",
    "GOOGLE_DEFAULT_DISCOVERY_DOCUMENT_URL",
    "GOOGLE_DEFAULT_ISSUER",
    "GOOGLE_DEFAULT_JWKS_URI",
    "GOOGLE_DEFAULT_SCOPES",
    "GOOGLE_DEFAULT_TOKEN_ENDPOINT",
    "GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE",
    "GOOGLE_OAUTH_NONCE_BYTES",
    "GOOGLE_OAUTH_STATE_BYTES",
    "GOOGLE_OAUTH_STATE_COOKIE",
    "GOOGLE_OAUTH_STATE_EXPIRY_SECONDS",
    "GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE",
    "GOOGLE_PROVIDER_NAME",
    "GoogleIDTokenClaims",
    "GoogleIDTokenValidationError",
    "GoogleIDTokenValidationRequest",
    "GoogleIDTokenValidator",
    "GoogleJwksClient",
    "GoogleOIDCIDTokenValidator",
    "GoogleOAuthTokenClient",
    "GoogleOAuthPurpose",
    "GoogleOAuthSettings",
    "GoogleOAuthState",
    "GoogleTokenClient",
    "GoogleTokenExchangeError",
    "GoogleTokenExchangeRequest",
    "GoogleTokenResponse",
    "create_google_oauth_state",
    "decode_google_oauth_state_cookie",
    "encode_google_oauth_state_cookie",
    "google_id_token_claims_from_payload",
    "google_oauth_settings_from_provider",
    "google_token_response_from_payload",
)
