from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from binascii import Error as BinasciiError
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from http.client import HTTPException as HTTPClientException
from http.client import HTTPSConnection
from secrets import token_urlsafe
from typing import Final, Literal, Protocol, cast, runtime_checkable
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit

from wybra.auth.timestamps import current_timestamp
from wybra.core.exceptions import ConfigurationError
from wybra.providers.settings import ProviderSettings
from wybra.services.secrets import SecretSource

GITHUB_PROVIDER_NAME: Final = "github"
GITHUB_API_CLIENT_STATE_ATTRIBUTE: Final = "github_api_client"
GITHUB_OAUTH_STATE_COOKIE: Final = "wybra_github_oauth_state"
GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE: Final = "github_oauth_token_client"
GITHUB_OAUTH_STATE_EXPIRY_SECONDS: Final[float] = 300.0
GITHUB_OAUTH_STATE_BYTES: Final = 32
GITHUB_PKCE_VERIFIER_BYTES: Final = 64
GITHUB_DEFAULT_SCOPES: Final = ("read:user", "user:email")
GITHUB_DEFAULT_AUTHORISATION_ENDPOINT: Final = (
    "https://github.com/login/oauth/authorize"
)
GITHUB_DEFAULT_TOKEN_ENDPOINT: Final = "https://github.com/login/oauth/access_token"
GITHUB_DEFAULT_USER_API_ENDPOINT: Final = "https://api.github.com/user"
GITHUB_DEFAULT_EMAILS_API_ENDPOINT: Final = "https://api.github.com/user/emails"
GITHUB_DEFAULT_API_VERSION: Final = "2022-11-28"
GITHUB_DEFAULT_USER_AGENT: Final = "wybra"
_STATE_COOKIE_SEPARATOR: Final = "."
GitHubOAuthPurpose = Literal["login", "link"]


@dataclass(frozen=True, slots=True)
class GitHubOAuthSettings:
    provider_name: str
    client_id: str
    client_secret_reference: tuple[SecretSource, str]
    scopes: tuple[str, ...] = GITHUB_DEFAULT_SCOPES
    authorisation_endpoint: str = GITHUB_DEFAULT_AUTHORISATION_ENDPOINT
    token_endpoint: str = GITHUB_DEFAULT_TOKEN_ENDPOINT
    user_api_endpoint: str = GITHUB_DEFAULT_USER_API_ENDPOINT
    emails_api_endpoint: str = GITHUB_DEFAULT_EMAILS_API_ENDPOINT
    api_version: str = GITHUB_DEFAULT_API_VERSION
    user_agent: str = GITHUB_DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True)
class GitHubTokenExchangeRequest:
    token_endpoint: str
    client_id: str
    client_secret: str = field(repr=False)
    code: str = field(repr=False)
    redirect_uri: str
    code_verifier: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GitHubTokenResponse:
    access_token: str | None = None
    token_type: str | None = None
    scope: str | None = None
    expires_in: int | None = None
    refresh_token: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict, repr=False)


class GitHubTokenExchangeError(RuntimeError):
    """Raised when GitHub authorisation-code token exchange fails."""


class GitHubAPIError(RuntimeError):
    """Raised when GitHub user identity cannot be trusted."""


@runtime_checkable
class GitHubTokenClient(Protocol):
    async def exchange_code(
        self,
        request: GitHubTokenExchangeRequest,
    ) -> GitHubTokenResponse: ...


@dataclass(frozen=True, slots=True)
class GitHubOAuthTokenClient:
    timeout: float = 10.0

    async def exchange_code(
        self,
        request: GitHubTokenExchangeRequest,
    ) -> GitHubTokenResponse:
        return await asyncio.to_thread(
            _exchange_github_authorisation_code,
            request,
            self.timeout,
        )


@dataclass(frozen=True, slots=True)
class GitHubIdentityRequest:
    settings: GitHubOAuthSettings
    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GitHubUserClaims:
    subject: str
    email: str
    email_verified: bool
    login: str | None = None
    claims: Mapping[str, object] = field(default_factory=dict, repr=False)


@runtime_checkable
class GitHubAPIClient(Protocol):
    async def fetch_identity(
        self,
        request: GitHubIdentityRequest,
    ) -> GitHubUserClaims: ...


@dataclass(frozen=True, slots=True)
class GitHubRESTAPIClient:
    timeout: float = 10.0

    async def fetch_identity(
        self,
        request: GitHubIdentityRequest,
    ) -> GitHubUserClaims:
        return await asyncio.to_thread(
            _fetch_github_identity,
            request,
            self.timeout,
        )


@dataclass(frozen=True, slots=True)
class GitHubOAuthState:
    provider_name: str
    purpose: GitHubOAuthPurpose
    state: str
    code_verifier: str
    return_to: str
    redirect_uri: str
    expires_at: float
    user_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_name != GITHUB_PROVIDER_NAME:
            raise ValueError("GitHub OAuth state requires provider 'github'.")
        if self.purpose not in ("login", "link"):
            raise ValueError("GitHub OAuth state purpose must be login or link.")
        for field_name in ("state", "code_verifier", "return_to", "redirect_uri"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"GitHub OAuth state {field_name} must be non-blank.")
        if self.expires_at <= 0:
            raise ValueError("GitHub OAuth state expiry must be positive.")
        if self.user_id is not None and not self.user_id.strip():
            raise ValueError("GitHub OAuth state user_id must be non-blank.")

    @property
    def code_challenge(self) -> str:
        return github_pkce_challenge(self.code_verifier)


def github_oauth_settings_from_provider(
    provider: ProviderSettings,
) -> GitHubOAuthSettings:
    if provider.name != GITHUB_PROVIDER_NAME:
        raise ConfigurationError("GitHub OAuth settings require provider 'github'.")
    if provider.client_id is None:
        raise ConfigurationError("GitHub provider must configure 'client_id'.")
    client_secret_reference = provider.required_client_secret_reference()
    if client_secret_reference is None:
        raise ConfigurationError(
            "GitHub provider must configure both 'secrets' and 'client_secret_key'."
        )
    return GitHubOAuthSettings(
        provider_name=provider.name,
        client_id=provider.client_id,
        client_secret_reference=client_secret_reference,
    )


def create_github_oauth_state(
    *,
    purpose: GitHubOAuthPurpose,
    return_to: str,
    redirect_uri: str,
    user_id: str | None = None,
    now: float | None = None,
) -> GitHubOAuthState:
    return GitHubOAuthState(
        provider_name=GITHUB_PROVIDER_NAME,
        purpose=purpose,
        state=token_urlsafe(GITHUB_OAUTH_STATE_BYTES),
        code_verifier=token_urlsafe(GITHUB_PKCE_VERIFIER_BYTES),
        return_to=return_to,
        redirect_uri=redirect_uri,
        expires_at=(current_timestamp() if now is None else now)
        + GITHUB_OAUTH_STATE_EXPIRY_SECONDS,
        user_id=user_id,
    )


def github_pkce_challenge(code_verifier: str) -> str:
    return _urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())


def github_token_response_from_payload(
    payload: Mapping[str, object],
) -> GitHubTokenResponse:
    return GitHubTokenResponse(
        access_token=_optional_payload_str(payload, "access_token"),
        token_type=_optional_payload_str(payload, "token_type"),
        scope=_optional_payload_str(payload, "scope"),
        expires_in=_optional_payload_int(payload, "expires_in"),
        refresh_token=_optional_payload_str(payload, "refresh_token"),
        raw=dict(payload),
    )


def github_granted_scopes(scope_value: str | None) -> tuple[str, ...]:
    if scope_value is None:
        return ()
    scopes: list[str] = []
    for comma_part in scope_value.split(","):
        for item in comma_part.split():
            if item.strip():
                scopes.append(item.strip())
    return tuple(dict.fromkeys(scopes))


def github_token_response_has_required_scopes(
    response: GitHubTokenResponse,
    required_scopes: Sequence[str],
) -> bool:
    return set(required_scopes).issubset(github_granted_scopes(response.scope))


def github_user_claims_from_api_payloads(
    user_payload: Mapping[str, object],
    emails_payload: Sequence[Mapping[str, object]],
) -> GitHubUserClaims:
    subject = _github_subject(user_payload.get("id"))
    login = _optional_mapping_str(user_payload, "login")
    selected_email = _selected_github_email(emails_payload)
    if selected_email is None:
        raise GitHubAPIError("GitHub account does not expose an email address.")
    email, email_verified = selected_email
    claims: dict[str, object] = {
        "id": subject,
        "email": email,
        "email_verified": email_verified,
    }
    if login is not None:
        claims["login"] = login
    for field_name in ("avatar_url", "html_url", "name"):
        value = _optional_mapping_str(user_payload, field_name)
        if value is not None:
            claims[field_name] = value
    return GitHubUserClaims(
        subject=subject,
        email=email,
        email_verified=email_verified,
        login=login,
        claims=claims,
    )


def encode_github_oauth_state_cookie(
    state: GitHubOAuthState,
    *,
    secret: str,
) -> str:
    payload = _urlsafe_b64encode(
        json.dumps(
            asdict(state),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{payload}{_STATE_COOKIE_SEPARATOR}{_signature(payload, secret)}"


def decode_github_oauth_state_cookie(
    value: str,
    *,
    secret: str,
    now: float | None = None,
) -> GitHubOAuthState | None:
    payload, separator, signature = value.partition(_STATE_COOKIE_SEPARATOR)
    if separator != _STATE_COOKIE_SEPARATOR or not payload or not signature:
        return None
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        return None

    try:
        raw_payload = json.loads(_urlsafe_b64decode(payload).decode("utf-8"))
    except (BinasciiError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_payload, dict):
        return None

    state = _github_oauth_state_from_payload(raw_payload)
    if state is None:
        return None
    if (current_timestamp() if now is None else now) > state.expires_at:
        return None
    return state


def _github_oauth_state_from_payload(
    payload: dict[object, object],
) -> GitHubOAuthState | None:
    try:
        provider_name = payload["provider_name"]
        purpose = payload["purpose"]
        state = payload["state"]
        code_verifier = payload["code_verifier"]
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
        and isinstance(code_verifier, str)
        and isinstance(return_to, str)
        and isinstance(redirect_uri, str)
        and isinstance(expires_at, (int, float))
        and (user_id is None or isinstance(user_id, str))
    ):
        return None
    try:
        return GitHubOAuthState(
            provider_name=provider_name,
            purpose=cast(GitHubOAuthPurpose, purpose),
            state=state,
            code_verifier=code_verifier,
            return_to=return_to,
            redirect_uri=redirect_uri,
            expires_at=float(expires_at),
            user_id=user_id,
        )
    except ValueError:
        return None


def _exchange_github_authorisation_code(
    request: GitHubTokenExchangeRequest,
    timeout: float,
) -> GitHubTokenResponse:
    parsed_endpoint = _github_https_endpoint(
        request.token_endpoint,
        error_type=GitHubTokenExchangeError,
        error_message="GitHub token endpoint must be HTTPS.",
    )
    body = urlencode(
        {
            "client_id": request.client_id,
            "client_secret": request.client_secret,
            "code": request.code,
            "code_verifier": request.code_verifier,
            "redirect_uri": request.redirect_uri,
        }
    ).encode("ascii")
    response_body = _github_https_request(
        parsed_endpoint,
        method="POST",
        body=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": GITHUB_DEFAULT_USER_AGENT,
        },
        timeout=timeout,
        error_type=GitHubTokenExchangeError,
        error_message="GitHub token exchange failed.",
    )
    payload = _json_object_response(
        response_body,
        error_type=GitHubTokenExchangeError,
        error_message="GitHub token exchange returned an invalid response.",
    )
    return github_token_response_from_payload(payload)


def _fetch_github_identity(
    request: GitHubIdentityRequest,
    timeout: float,
) -> GitHubUserClaims:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {request.access_token}",
        "User-Agent": request.settings.user_agent,
        "X-GitHub-Api-Version": request.settings.api_version,
    }
    user_body = _github_https_request(
        _github_https_endpoint(
            request.settings.user_api_endpoint,
            error_type=GitHubAPIError,
            error_message="GitHub endpoint must be HTTPS.",
        ),
        method="GET",
        body=None,
        headers=headers,
        timeout=timeout,
        error_type=GitHubAPIError,
        error_message="GitHub user API request failed.",
    )
    emails_body = _github_https_request(
        _github_https_endpoint(
            request.settings.emails_api_endpoint,
            error_type=GitHubAPIError,
            error_message="GitHub endpoint must be HTTPS.",
        ),
        method="GET",
        body=None,
        headers=headers,
        timeout=timeout,
        error_type=GitHubAPIError,
        error_message="GitHub emails API request failed.",
    )
    user_payload = _json_object_response(
        user_body,
        error_type=GitHubAPIError,
        error_message="GitHub user API returned an invalid response.",
    )
    emails_payload = _json_array_response(
        emails_body,
        error_type=GitHubAPIError,
        error_message="GitHub emails API returned an invalid response.",
    )
    return github_user_claims_from_api_payloads(
        user_payload,
        tuple(_mapping_items(emails_payload)),
    )


def _github_https_request(
    parsed_endpoint: SplitResult,
    *,
    method: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
    error_type: type[Exception],
    error_message: str,
) -> bytes:
    hostname = parsed_endpoint.hostname
    if hostname is None:
        raise error_type(error_message)
    connection = HTTPSConnection(
        hostname,
        parsed_endpoint.port,
        timeout=timeout,
    )
    try:
        connection.request(
            method,
            urlunsplit(
                ("", "", parsed_endpoint.path or "/", parsed_endpoint.query, "")
            ),
            body=body,
            headers=dict(headers),
        )
        response = connection.getresponse()
        response_body = response.read()
    except (HTTPClientException, OSError, TimeoutError) as exc:
        raise error_type(error_message) from exc
    finally:
        connection.close()

    if response.status < 200 or response.status >= 300:
        raise error_type(error_message)
    return response_body


def _github_https_endpoint(
    value: str,
    *,
    error_type: type[Exception],
    error_message: str,
) -> SplitResult:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise error_type(error_message)
    return parsed


def _json_object_response(
    response_body: bytes,
    *,
    error_type: type[Exception],
    error_message: str,
) -> Mapping[str, object]:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(error_message) from exc
    if not isinstance(payload, dict):
        raise error_type(error_message)
    return cast(Mapping[str, object], payload)


def _json_array_response(
    response_body: bytes,
    *,
    error_type: type[Exception],
    error_message: str,
) -> Sequence[object]:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(error_message) from exc
    if not isinstance(payload, list):
        raise error_type(error_message)
    return payload


def _mapping_items(items: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    mappings: list[Mapping[str, object]] = []
    for item in items:
        if isinstance(item, Mapping):
            mappings.append(cast(Mapping[str, object], item))
    return tuple(mappings)


def _optional_payload_str(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise GitHubTokenExchangeError(
        f"GitHub token exchange response field {field_name!r} must be a string."
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
    raise GitHubTokenExchangeError(
        f"GitHub token exchange response field {field_name!r} must be an integer."
    )


def _github_subject(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise GitHubAPIError("GitHub user id is invalid.")


def _optional_mapping_str(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _selected_github_email(
    emails_payload: Sequence[Mapping[str, object]],
) -> tuple[str, bool] | None:
    candidates: list[tuple[str, bool, bool]] = []
    for item in emails_payload:
        email = item.get("email")
        if not isinstance(email, str) or not email.strip():
            continue
        verified = item.get("verified") is True
        primary = item.get("primary") is True
        candidates.append((email.strip(), verified, primary))
    for email, verified, primary in candidates:
        if verified and primary:
            return email, True
    for email, verified, _primary in candidates:
        if verified:
            return email, True
    for email, verified, primary in candidates:
        if primary:
            return email, verified
    if candidates:
        email, verified, _primary = candidates[0]
        return email, verified
    return None


def _signature(payload: str, secret: str) -> str:
    return _urlsafe_b64encode(
        hmac.new(
            secret.encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


__all__ = (
    "GITHUB_API_CLIENT_STATE_ATTRIBUTE",
    "GITHUB_DEFAULT_API_VERSION",
    "GITHUB_DEFAULT_AUTHORISATION_ENDPOINT",
    "GITHUB_DEFAULT_EMAILS_API_ENDPOINT",
    "GITHUB_DEFAULT_SCOPES",
    "GITHUB_DEFAULT_TOKEN_ENDPOINT",
    "GITHUB_DEFAULT_USER_AGENT",
    "GITHUB_DEFAULT_USER_API_ENDPOINT",
    "GITHUB_OAUTH_STATE_BYTES",
    "GITHUB_OAUTH_STATE_COOKIE",
    "GITHUB_OAUTH_STATE_EXPIRY_SECONDS",
    "GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE",
    "GITHUB_PKCE_VERIFIER_BYTES",
    "GITHUB_PROVIDER_NAME",
    "GitHubAPIClient",
    "GitHubAPIError",
    "GitHubIdentityRequest",
    "GitHubOAuthPurpose",
    "GitHubOAuthSettings",
    "GitHubOAuthState",
    "GitHubOAuthTokenClient",
    "GitHubRESTAPIClient",
    "GitHubTokenClient",
    "GitHubTokenExchangeError",
    "GitHubTokenExchangeRequest",
    "GitHubTokenResponse",
    "GitHubUserClaims",
    "create_github_oauth_state",
    "decode_github_oauth_state_cookie",
    "encode_github_oauth_state_cookie",
    "github_granted_scopes",
    "github_oauth_settings_from_provider",
    "github_pkce_challenge",
    "github_token_response_from_payload",
    "github_token_response_has_required_scopes",
    "github_user_claims_from_api_payloads",
)
