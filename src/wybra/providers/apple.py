from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from secrets import token_urlsafe
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlencode

import jwt
from jwt import PyJWKClient, PyJWTError

from wybra.auth.timestamps import current_timestamp
from wybra.core.exceptions import ConfigurationError
from wybra.providers.flow import ProviderOAuthPurpose, is_provider_oauth_purpose
from wybra.providers.http import https_endpoint, https_request, json_object_response
from wybra.providers.oauth_state import (
    decode_signed_oauth_state,
    encode_signed_oauth_state,
)
from wybra.providers.oidc import oidc_id_token_payload
from wybra.providers.settings import (
    APPLE_PROVIDER_NAME,
    PROVIDER_CLIENT_ID_FIELD,
    PROVIDER_KEY_ID_FIELD,
    PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD,
    PROVIDER_SECRETS_FIELD,
    PROVIDER_TEAM_ID_FIELD,
    ProviderSettings,
)
from wybra.services.secrets import SecretSource

APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE: Final = "apple_id_token_validator"
APPLE_OAUTH_STATE_COOKIE: Final = "wybra_apple_oauth_state"
APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE: Final = "apple_oauth_token_client"
APPLE_OAUTH_STATE_EXPIRY_SECONDS: Final[float] = 300.0
APPLE_OAUTH_STATE_BYTES: Final = 32
APPLE_OAUTH_NONCE_BYTES: Final = 32
APPLE_DEFAULT_SCOPES: Final = ("name", "email")
APPLE_DEFAULT_ISSUER: Final = "https://appleid.apple.com"
APPLE_DEFAULT_AUTHORISATION_ENDPOINT: Final = "https://appleid.apple.com/auth/authorize"
APPLE_DEFAULT_TOKEN_ENDPOINT: Final = "https://appleid.apple.com/auth/token"
APPLE_DEFAULT_JWKS_URI: Final = "https://appleid.apple.com/auth/keys"
APPLE_CLIENT_SECRET_AUDIENCE: Final = "https://appleid.apple.com"
APPLE_CLIENT_SECRET_LIFETIME_SECONDS: Final = 300
AppleOAuthPurpose = ProviderOAuthPurpose
AppleJwksClientFactory = Callable[[str], "AppleJwksClient"]


@dataclass(frozen=True, slots=True)
class AppleOAuthSettings:
    provider_name: str
    client_id: str
    team_id: str
    key_id: str
    private_key_reference: tuple[SecretSource, str]
    scopes: tuple[str, ...] = APPLE_DEFAULT_SCOPES
    issuer: str = APPLE_DEFAULT_ISSUER
    authorisation_endpoint: str = APPLE_DEFAULT_AUTHORISATION_ENDPOINT
    token_endpoint: str = APPLE_DEFAULT_TOKEN_ENDPOINT
    jwks_uri: str = APPLE_DEFAULT_JWKS_URI


@dataclass(frozen=True, slots=True)
class AppleTokenExchangeRequest:
    token_endpoint: str
    client_id: str
    client_secret: str = field(repr=False)
    code: str = field(repr=False)
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AppleTokenResponse:
    access_token: str | None = None
    id_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    raw: Mapping[str, object] = field(default_factory=dict, repr=False)


class AppleTokenExchangeError(RuntimeError):
    """Raised when Apple authorisation-code token exchange fails."""


class AppleIDTokenValidationError(RuntimeError):
    """Raised when an Apple ID token cannot be trusted."""


class AppleClientSecretError(RuntimeError):
    """Raised when an Apple client secret cannot be generated."""


class AppleJwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@runtime_checkable
class AppleTokenClient(Protocol):
    async def exchange_code(
        self,
        request: AppleTokenExchangeRequest,
    ) -> AppleTokenResponse: ...


@dataclass(frozen=True, slots=True)
class AppleOAuthTokenClient:
    timeout: float = 10.0

    async def exchange_code(
        self,
        request: AppleTokenExchangeRequest,
    ) -> AppleTokenResponse:
        return await asyncio.to_thread(
            _exchange_apple_authorisation_code,
            request,
            self.timeout,
        )


@dataclass(frozen=True, slots=True)
class AppleIDTokenValidationRequest:
    id_token: str = field(repr=False)
    settings: AppleOAuthSettings
    nonce: str


@dataclass(frozen=True, slots=True)
class AppleIDTokenClaims:
    subject: str
    email: str
    email_verified: bool
    nonce: str
    claims: Mapping[str, object] = field(default_factory=dict, repr=False)


@runtime_checkable
class AppleIDTokenValidator(Protocol):
    async def validate(
        self,
        request: AppleIDTokenValidationRequest,
    ) -> AppleIDTokenClaims: ...


@dataclass(frozen=True, slots=True)
class AppleOIDCIDTokenValidator:
    jwks_client_factory: AppleJwksClientFactory = PyJWKClient

    async def validate(
        self,
        request: AppleIDTokenValidationRequest,
    ) -> AppleIDTokenClaims:
        return await asyncio.to_thread(
            _validate_apple_id_token,
            request,
            self.jwks_client_factory,
        )


@dataclass(frozen=True, slots=True)
class AppleOAuthState:
    provider_name: str
    purpose: AppleOAuthPurpose
    state: str
    nonce: str
    return_to: str
    redirect_uri: str
    expires_at: float
    user_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_name != APPLE_PROVIDER_NAME:
            raise ValueError(
                f"Apple OAuth state requires provider {APPLE_PROVIDER_NAME!r}."
            )
        if not is_provider_oauth_purpose(self.purpose):
            raise ValueError("Apple OAuth state purpose must be login or link.")
        for field_name in ("state", "nonce", "return_to", "redirect_uri"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Apple OAuth state {field_name} must be non-blank.")
        if self.expires_at <= 0:
            raise ValueError("Apple OAuth state expiry must be positive.")
        if self.user_id is not None and not self.user_id.strip():
            raise ValueError("Apple OAuth state user_id must be non-blank.")


def apple_oauth_settings_from_provider(
    provider: ProviderSettings,
) -> AppleOAuthSettings:
    if provider.name != APPLE_PROVIDER_NAME:
        raise ConfigurationError(
            f"Apple OAuth settings require provider {APPLE_PROVIDER_NAME!r}."
        )
    if provider.client_id is None:
        raise ConfigurationError(
            f"Apple provider must configure {PROVIDER_CLIENT_ID_FIELD!r}."
        )
    if provider.team_id is None:
        raise ConfigurationError(
            f"Apple provider must configure {PROVIDER_TEAM_ID_FIELD!r}."
        )
    if provider.key_id is None:
        raise ConfigurationError(
            f"Apple provider must configure {PROVIDER_KEY_ID_FIELD!r}."
        )
    private_key_reference = provider.required_private_key_reference()
    if private_key_reference is None:
        raise ConfigurationError(
            "Apple provider must configure both "
            f"{PROVIDER_SECRETS_FIELD!r} and "
            f"{PROVIDER_PRIVATE_KEY_SECRET_KEY_FIELD!r}."
        )
    return AppleOAuthSettings(
        provider_name=provider.name,
        client_id=provider.client_id,
        team_id=provider.team_id,
        key_id=provider.key_id,
        private_key_reference=private_key_reference,
    )


def create_apple_oauth_state(
    *,
    purpose: AppleOAuthPurpose,
    return_to: str,
    redirect_uri: str,
    user_id: str | None = None,
    now: float | None = None,
) -> AppleOAuthState:
    return AppleOAuthState(
        provider_name=APPLE_PROVIDER_NAME,
        purpose=purpose,
        state=token_urlsafe(APPLE_OAUTH_STATE_BYTES),
        nonce=token_urlsafe(APPLE_OAUTH_NONCE_BYTES),
        return_to=return_to,
        redirect_uri=redirect_uri,
        expires_at=(current_timestamp() if now is None else now)
        + APPLE_OAUTH_STATE_EXPIRY_SECONDS,
        user_id=user_id,
    )


def create_apple_client_secret(
    settings: AppleOAuthSettings,
    *,
    private_key: str,
    now: float | None = None,
    lifetime_seconds: int = APPLE_CLIENT_SECRET_LIFETIME_SECONDS,
) -> str:
    if not isinstance(private_key, str) or not private_key.strip():
        raise AppleClientSecretError("Apple private key is missing.")
    issued_at = int(current_timestamp() if now is None else now)
    expires_at = issued_at + lifetime_seconds
    try:
        return jwt.encode(
            {
                "iss": settings.team_id,
                "iat": issued_at,
                "exp": expires_at,
                "aud": APPLE_CLIENT_SECRET_AUDIENCE,
                "sub": settings.client_id,
            },
            private_key,
            algorithm="ES256",
            headers={"kid": settings.key_id},
        )
    except (PyJWTError, TypeError, ValueError) as exc:
        raise AppleClientSecretError("Apple client secret is invalid.") from exc


def apple_token_response_from_payload(
    payload: Mapping[str, object],
) -> AppleTokenResponse:
    return AppleTokenResponse(
        access_token=_optional_payload_str(payload, "access_token"),
        id_token=_optional_payload_str(payload, "id_token"),
        refresh_token=_optional_payload_str(payload, "refresh_token"),
        token_type=_optional_payload_str(payload, "token_type"),
        expires_in=_optional_payload_int(payload, "expires_in"),
        raw=dict(payload),
    )


def apple_id_token_claims_from_payload(
    payload: Mapping[str, object],
    *,
    expected_nonce: str,
) -> AppleIDTokenClaims:
    subject = _required_payload_str(payload, "sub")
    email = _required_payload_str(payload, "email")
    email_verified = _required_payload_boolish(payload, "email_verified")
    nonce = _required_payload_str(payload, "nonce")
    if not hmac.compare_digest(nonce, expected_nonce):
        raise AppleIDTokenValidationError("Apple ID token nonce is invalid.")
    return AppleIDTokenClaims(
        subject=subject,
        email=email,
        email_verified=email_verified,
        nonce=nonce,
        claims=dict(payload),
    )


def encode_apple_oauth_state_cookie(
    state: AppleOAuthState,
    *,
    secret: str,
) -> str:
    return encode_signed_oauth_state(asdict(state), secret=secret)


def decode_apple_oauth_state_cookie(
    value: str,
    *,
    secret: str,
    now: float | None = None,
) -> AppleOAuthState | None:
    return decode_signed_oauth_state(
        value,
        secret=secret,
        state_factory=_apple_oauth_state_from_payload,
        now=now,
    )


def _apple_oauth_state_from_payload(
    payload: dict[object, object],
) -> AppleOAuthState | None:
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
        and is_provider_oauth_purpose(purpose)
        and isinstance(state, str)
        and isinstance(nonce, str)
        and isinstance(return_to, str)
        and isinstance(redirect_uri, str)
        and isinstance(expires_at, (int, float))
        and (user_id is None or isinstance(user_id, str))
    ):
        return None
    try:
        return AppleOAuthState(
            provider_name=provider_name,
            purpose=purpose,
            state=state,
            nonce=nonce,
            return_to=return_to,
            redirect_uri=redirect_uri,
            expires_at=float(expires_at),
            user_id=user_id,
        )
    except ValueError:
        return None


def _exchange_apple_authorisation_code(
    request: AppleTokenExchangeRequest,
    timeout: float,
) -> AppleTokenResponse:
    parsed_endpoint = https_endpoint(
        request.token_endpoint,
        error_type=AppleTokenExchangeError,
        error_message="Apple token endpoint must be HTTPS.",
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
        error_type=AppleTokenExchangeError,
        error_message="Apple token exchange failed.",
    )
    payload = json_object_response(
        response_body,
        error_type=AppleTokenExchangeError,
        invalid_json_message="Apple token exchange returned invalid JSON.",
        invalid_payload_message="Apple token exchange returned an invalid response.",
    )
    return apple_token_response_from_payload(payload)


def _validate_apple_id_token(
    request: AppleIDTokenValidationRequest,
    jwks_client_factory: AppleJwksClientFactory,
) -> AppleIDTokenClaims:
    payload = oidc_id_token_payload(
        request.id_token,
        jwks_uri=request.settings.jwks_uri,
        audience=request.settings.client_id,
        issuer=request.settings.issuer,
        jwks_client_factory=jwks_client_factory,
        error_type=AppleIDTokenValidationError,
        missing_message="Apple ID token is missing.",
        invalid_message="Apple ID token is invalid.",
        invalid_payload_message="Apple ID token payload is invalid.",
    )
    return apple_id_token_claims_from_payload(
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
    raise AppleTokenExchangeError(
        f"Apple token exchange response field {field_name!r} must be a string."
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
    raise AppleTokenExchangeError(
        f"Apple token exchange response field {field_name!r} must be an integer."
    )


def _required_payload_str(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise AppleIDTokenValidationError(
        f"Apple ID token claim {field_name!r} must be a non-blank string."
    )


def _required_payload_boolish(
    payload: Mapping[str, object],
    field_name: str,
) -> bool:
    value = payload.get(field_name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise AppleIDTokenValidationError(
        f"Apple ID token claim {field_name!r} must be a boolean."
    )


__all__ = (
    "APPLE_CLIENT_SECRET_AUDIENCE",
    "APPLE_CLIENT_SECRET_LIFETIME_SECONDS",
    "APPLE_DEFAULT_AUTHORISATION_ENDPOINT",
    "APPLE_DEFAULT_ISSUER",
    "APPLE_DEFAULT_JWKS_URI",
    "APPLE_DEFAULT_SCOPES",
    "APPLE_DEFAULT_TOKEN_ENDPOINT",
    "APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE",
    "APPLE_OAUTH_NONCE_BYTES",
    "APPLE_OAUTH_STATE_BYTES",
    "APPLE_OAUTH_STATE_COOKIE",
    "APPLE_OAUTH_STATE_EXPIRY_SECONDS",
    "APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE",
    "APPLE_PROVIDER_NAME",
    "AppleClientSecretError",
    "AppleIDTokenClaims",
    "AppleIDTokenValidationError",
    "AppleIDTokenValidationRequest",
    "AppleIDTokenValidator",
    "AppleJwksClient",
    "AppleOIDCIDTokenValidator",
    "AppleOAuthPurpose",
    "AppleOAuthSettings",
    "AppleOAuthState",
    "AppleOAuthTokenClient",
    "AppleTokenClient",
    "AppleTokenExchangeError",
    "AppleTokenExchangeRequest",
    "AppleTokenResponse",
    "apple_id_token_claims_from_payload",
    "apple_oauth_settings_from_provider",
    "apple_token_response_from_payload",
    "create_apple_client_secret",
    "create_apple_oauth_state",
    "decode_apple_oauth_state_cookie",
    "encode_apple_oauth_state_cookie",
)
