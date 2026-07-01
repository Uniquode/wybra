import re
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from wybra.auth import AuthCapability, anonymous_required, login_required
from wybra.auth.accounts.manager import create_user_manager
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.delivery import NullIdentityDelivery
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    SqlAlchemyRecoveryCodeStore,
    SqlAlchemyTOTPCredentialStore,
)
from wybra.auth.mfa.totp import generate_totp, generate_totp_secret
from wybra.auth.models import ExternalIdentityLink, IdentityProvider, User
from wybra.auth.models import metadata as auth_metadata
from wybra.auth.provider_credentials import SqlAlchemyProviderCredentialStore
from wybra.auth.routes.pages import totp_management as totp_management_pages
from wybra.auth.routes.totp import TOTP_LOGIN_NONCE_COOKIE
from wybra.config import MappingConfigSource
from wybra.db import DatabaseCapability
from wybra.providers.github import (
    GITHUB_API_CLIENT_STATE_ATTRIBUTE,
    GITHUB_OAUTH_STATE_COOKIE,
    GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GitHubAPIError,
    GitHubIdentityRequest,
    GitHubOAuthState,
    GitHubTokenExchangeError,
    GitHubTokenExchangeRequest,
    GitHubTokenResponse,
    GitHubUserClaims,
    decode_github_oauth_state_cookie,
)
from wybra.providers.google import (
    GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
    GOOGLE_OAUTH_STATE_COOKIE,
    GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GoogleIDTokenClaims,
    GoogleIDTokenValidationError,
    GoogleIDTokenValidationRequest,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
    GoogleTokenResponse,
    create_google_oauth_state,
    decode_google_oauth_state_cookie,
    encode_google_oauth_state_cookie,
)
from wybra.services.crypto import SecretEnvelopeService
from wybra.site import Site, SiteCapabilityError, start

PAGE_MODULES = (
    "wybra.forms",
    "wybra.assets",
    "wybra.template",
    "wybra.db",
    "wybra.auth",
)
STRONG_TEST_PASSWORD = "Correct horse 42!"


@pytest.fixture(autouse=True)
async def close_started_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    started_sites: list[Site] = []
    original_start = start

    async def tracked_start(*args, **kwargs) -> Site:
        site = await original_start(*args, **kwargs)
        started_sites.append(site)
        return site

    monkeypatch.setattr(sys.modules[__name__], "start", tracked_start)
    try:
        yield
    finally:
        for site in reversed(started_sites):
            await site.close()


def _site_config_source(
    tmp_path: Path,
    *,
    modules: tuple[str, ...] = ("wybra.forms", "wybra.db", "wybra.auth"),
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
    provider_route_prefix: str | None = None,
    provider_route_prefixes: dict[str, str] | None = None,
    providers_config: dict[str, object] | None = None,
) -> MappingConfigSource:
    route_prefixes: dict[str, dict[str, str]] = {
        "wybra.auth": {"account": account_prefix, "api": ""},
    }
    if provider_route_prefixes is not None:
        route_prefixes["wybra.providers"] = provider_route_prefixes
    elif provider_route_prefix is not None:
        route_prefixes["wybra.providers"] = {"google": provider_route_prefix}

    config: dict[str, object] = {
        "app": {
            "config_path": tmp_path / "app.toml",
            "project_root": tmp_path,
            "modules": modules,
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
        },
        "app.routes": {"prefixes": route_prefixes},
        "app.templates": {"auto_reload": True, "cache_size": 0},
        "app.assets": {"url_path": "/static/", "root": Path("static")},
    }
    if auth_config is not None:
        config["auth"] = auth_config
    if providers_config is not None:
        config["auth.providers"] = providers_config
    return MappingConfigSource(config)


async def _create_auth_schema(site) -> None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:

        def create_all(sync_session) -> None:
            auth_metadata.create_all(sync_session.get_bind())

        await db_session.run_sync(create_all)


async def _create_active_totp_credential(
    site,
    user_id: uuid.UUID,
) -> tuple[str, tuple[str, ...]]:
    secret_service = SecretEnvelopeService.for_testing()
    site.app.state.secret_envelope_service = secret_service
    secret = generate_totp_secret()
    recovery_codes = generate_recovery_codes()
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = SqlAlchemyTOTPCredentialStore(
            db_session,
            secret_service,
        )
        credential_id = await store.create_pending_totp_credential(
            str(user_id),
            secret,
        )
        await store.activate_totp_credential(credential_id)
        recovery_store = SqlAlchemyRecoveryCodeStore(db_session, secret_service)
        await recovery_store.replace_recovery_codes(
            str(user_id),
            credential_id,
            recovery_codes,
        )
    return secret, recovery_codes


async def _active_totp_credential_id(site, user_id: uuid.UUID) -> str | None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = SqlAlchemyTOTPCredentialStore(
            db_session,
            site.app.state.secret_envelope_service,
        )
        return await store.get_active_totp_credential(str(user_id))


def _authenticated_security_site(
    tmp_path: Path,
    *,
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
):
    app = FastAPI()
    return app, _site_config_source(
        tmp_path,
        modules=PAGE_MODULES,
        auth_config=auth_config,
        account_prefix=account_prefix,
    )


def _override_current_user(
    app: FastAPI,
    user_id: uuid.UUID | None = None,
    *,
    email: str = "security@example.test",
    hashed_password: str | None = "hash",
    password_login_enabled: bool = True,
) -> None:
    async def current_user() -> SimpleNamespace:
        return SimpleNamespace(
            id=user_id or uuid.uuid4(),
            email=email,
            hashed_password=hashed_password,
            is_active=True,
            password_login_enabled=password_login_enabled,
            is_verified=True,
        )

    app.dependency_overrides[login_required] = current_user


def _security_page_client(site) -> TestClient:
    return TestClient(site.app)


def _csrf_token(response_text: str) -> str:
    match = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _assert_recovery_codes_download(response_text: str) -> None:
    recovery_code_match = re.search(r"<li><code>([^<]+)</code></li>", response_text)
    href_match = re.search(
        r'href="(data:text/plain;charset=utf-8,[^"]+)"',
        response_text,
    )
    assert recovery_code_match is not None
    assert href_match is not None
    assert 'download="recovery-codes.txt"' in response_text
    assert recovery_code_match.group(1) in href_match.group(1)


async def _create_local_user(
    site,
    *,
    email: str,
    password: str = STRONG_TEST_PASSWORD,
    is_verified: bool,
) -> uuid.UUID:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        manager = create_user_manager(
            db_session,
            site.app.state.auth_settings.identity_options,
        )
        user = await manager.create(
            UserCreate(email=email, password=password),
            safe=True,
        )
        user.is_verified = is_verified
        return user.id


async def _set_password_login_enabled(
    site,
    user_id: uuid.UUID,
    enabled: bool,
) -> None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        user = await db_session.get(User, user_id)
        assert user is not None
        user.password_login_enabled = enabled


async def _password_login_enabled(site, user_id: uuid.UUID) -> bool:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        user = await db_session.get(User, user_id)
        assert user is not None
        return user.password_login_enabled


async def _start_security_site(
    tmp_path: Path,
    *,
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
    user_id: uuid.UUID | None = None,
):
    app, config_source = _authenticated_security_site(
        tmp_path,
        auth_config=auth_config,
        account_prefix=account_prefix,
    )
    site = await start(app, config_source=config_source)
    _override_current_user(site.app, user_id=user_id)
    return site


def _google_provider_config(
    *,
    enabled: bool = True,
    secret_key: str | None = "GOOGLE_SECRET",
    account_creation_enabled: bool = False,
    email_match_linking_enabled: bool = False,
) -> dict[str, object]:
    config: dict[str, object] = {
        "enabled": enabled,
        "client_id": "google-client-id",
        "account_creation_enabled": account_creation_enabled,
        "email_match_linking_enabled": email_match_linking_enabled,
    }
    if secret_key is not None:
        config.update(
            {
                "secrets": "environment",
                "client_secret_key": secret_key,
            }
        )
    return {"google": config}


def _github_provider_config(
    *,
    enabled: bool = True,
    secret_key: str | None = "GITHUB_SECRET",
    account_creation_enabled: bool = False,
    email_match_linking_enabled: bool = False,
) -> dict[str, object]:
    config: dict[str, object] = {
        "enabled": enabled,
        "client_id": "github-client-id",
        "account_creation_enabled": account_creation_enabled,
        "email_match_linking_enabled": email_match_linking_enabled,
        "required_claims": ["id", "email", "email_verified"],
    }
    if secret_key is not None:
        config.update(
            {
                "secrets": "environment",
                "client_secret_key": secret_key,
            }
        )
    return {"github": config}


async def _start_google_provider_site(
    tmp_path: Path,
    *,
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
    provider_route_prefix: str | None = None,
    providers_config: dict[str, object] | None = None,
):
    return await start(
        FastAPI(),
        config_source=_site_config_source(
            tmp_path,
            modules=(
                "wybra.secrets",
                "wybra.forms",
                "wybra.assets",
                "wybra.template",
                "wybra.db",
                "wybra.auth",
                "wybra.providers",
            ),
            account_prefix=account_prefix,
            auth_config=auth_config,
            provider_route_prefix=provider_route_prefix
            or f"{account_prefix}/providers/google",
            providers_config=providers_config or _google_provider_config(),
        ),
    )


async def _start_github_provider_site(
    tmp_path: Path,
    *,
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
    provider_route_prefix: str | None = None,
    providers_config: dict[str, object] | None = None,
):
    return await start(
        FastAPI(),
        config_source=_site_config_source(
            tmp_path,
            modules=(
                "wybra.secrets",
                "wybra.forms",
                "wybra.assets",
                "wybra.template",
                "wybra.db",
                "wybra.auth",
                "wybra.providers",
            ),
            account_prefix=account_prefix,
            auth_config=auth_config,
            provider_route_prefixes={
                "github": provider_route_prefix or f"{account_prefix}/providers/github",
            },
            providers_config=providers_config or _github_provider_config(),
        ),
    )


def _google_oauth_cookie_state(site, response) -> object:
    cookie = response.cookies.get(GOOGLE_OAUTH_STATE_COOKIE)
    assert cookie is not None
    state = decode_google_oauth_state_cookie(
        cookie,
        secret=site.app.state.auth_settings.identity_options.verification_token_secret,
    )
    assert state is not None
    return state


def _github_oauth_cookie_state(site, response) -> GitHubOAuthState:
    cookie = response.cookies.get(GITHUB_OAUTH_STATE_COOKIE)
    assert cookie is not None
    state = decode_github_oauth_state_cookie(
        cookie,
        secret=site.app.state.auth_settings.identity_options.verification_token_secret,
    )
    assert state is not None
    return state


def _google_state_cookie_secret(site) -> str:
    return site.app.state.auth_settings.identity_options.verification_token_secret


async def _create_google_provider_link(
    site,
    *,
    user_id: uuid.UUID,
    provider_subject: str = "google-subject",
    account_email: str = "user@example.com",
) -> str:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        store = SqlAlchemyProviderCredentialStore(
            session,
            SecretEnvelopeService.for_testing(),
        )
        provider_id = await store.create_provider_credential(
            provider_name="google",
            provider_subject=provider_subject,
            access_token="stored-access-token",
            account_email=account_email,
        )
        await store.link_provider_to_user(
            provider_id=provider_id,
            user_id=user_id,
        )
        return provider_id


async def _create_github_provider_link(
    site,
    *,
    user_id: uuid.UUID,
    provider_subject: str = "github-subject",
    account_email: str = "user@example.com",
) -> str:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        store = SqlAlchemyProviderCredentialStore(
            session,
            SecretEnvelopeService.for_testing(),
        )
        provider_id = await store.create_provider_credential(
            provider_name="github",
            provider_subject=provider_subject,
            access_token="stored-access-token",
            account_email=account_email,
        )
        await store.link_provider_to_user(
            provider_id=provider_id,
            user_id=user_id,
        )
        return provider_id


async def _google_provider_linked_user_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        result = await session.execute(
            select(ExternalIdentityLink.user_id)
            .join(IdentityProvider)
            .where(
                IdentityProvider.provider_name == "google",
                IdentityProvider.provider_subject == provider_subject,
            )
        )
        return result.scalar_one_or_none()


async def _github_provider_linked_user_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        result = await session.execute(
            select(ExternalIdentityLink.user_id)
            .join(IdentityProvider)
            .where(
                IdentityProvider.provider_name == "github",
                IdentityProvider.provider_subject == provider_subject,
            )
        )
        return result.scalar_one_or_none()


async def _google_provider_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        result = await session.execute(
            select(IdentityProvider.id).where(
                IdentityProvider.provider_name == "google",
                IdentityProvider.provider_subject == provider_subject,
            )
        )
        return result.scalar_one_or_none()


async def _google_provider_subjects_for_user(
    site,
    *,
    user_id: uuid.UUID,
) -> tuple[str, ...]:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        result = await session.execute(
            select(IdentityProvider.provider_subject)
            .join(ExternalIdentityLink)
            .where(
                ExternalIdentityLink.user_id == user_id,
                IdentityProvider.provider_name == "google",
            )
            .order_by(IdentityProvider.provider_subject)
        )
        return tuple(result.scalars().all())


async def _google_user_by_email(site, email: str) -> User | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.unique().scalar_one_or_none()


def _authenticated_client(
    site,
    *,
    email: str,
    password: str = STRONG_TEST_PASSWORD,
) -> TestClient:
    client = TestClient(site.app)
    login_page = client.get("/account/login")
    response = client.post(
        "/account/login",
        data={
            "csrf_token": _csrf_token(login_page.text),
            "email": email,
            "password": password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


class FakeGoogleTokenClient:
    def __init__(
        self,
        response: GoogleTokenResponse | None = None,
        error: GoogleTokenExchangeError | None = None,
    ) -> None:
        self.response = response or GoogleTokenResponse(id_token="id-token")
        self.error = error
        self.requests: list[GoogleTokenExchangeRequest] = []

    async def exchange_code(
        self,
        request: GoogleTokenExchangeRequest,
    ) -> GoogleTokenResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeGoogleIDTokenValidator:
    def __init__(
        self,
        claims: GoogleIDTokenClaims | None = None,
        error: GoogleIDTokenValidationError | None = None,
    ) -> None:
        self.claims = claims or GoogleIDTokenClaims(
            subject="google-subject",
            email="user@example.com",
            email_verified=True,
            nonce="nonce",
        )
        self.error = error
        self.requests: list[GoogleIDTokenValidationRequest] = []

    async def validate(
        self,
        request: GoogleIDTokenValidationRequest,
    ) -> GoogleIDTokenClaims:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.claims


class FakeGitHubTokenClient:
    def __init__(
        self,
        response: GitHubTokenResponse | None = None,
        error: GitHubTokenExchangeError | None = None,
    ) -> None:
        self.response = response or GitHubTokenResponse(
            access_token="access-token",
            token_type="bearer",
            scope="read:user,user:email",
        )
        self.error = error
        self.requests: list[GitHubTokenExchangeRequest] = []

    async def exchange_code(
        self,
        request: GitHubTokenExchangeRequest,
    ) -> GitHubTokenResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeGitHubAPIClient:
    def __init__(
        self,
        claims: GitHubUserClaims | None = None,
        error: GitHubAPIError | None = None,
    ) -> None:
        self.claims = claims or GitHubUserClaims(
            subject="github-subject",
            email="user@example.com",
            email_verified=True,
            login="octocat",
            claims={
                "id": "github-subject",
                "email": "user@example.com",
                "email_verified": True,
                "login": "octocat",
            },
        )
        self.error = error
        self.requests: list[GitHubIdentityRequest] = []

    async def fetch_identity(
        self,
        request: GitHubIdentityRequest,
    ) -> GitHubUserClaims:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.claims


@pytest.mark.anyio
async def test_wybra_auth_setup_site_registers_auth_capability(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    site = await start(app, config_source=_site_config_source(tmp_path))

    auth = site.require_capability(AuthCapability)

    assert site.has_capability(AuthCapability) is True
    assert isinstance(auth, AuthCapability)
    assert auth.settings is app.state.auth_settings
    assert auth.fastapi_users is app.state.fastapi_users
    assert isinstance(app.state.identity_delivery, NullIdentityDelivery)
    assert callable(auth.optional_current_user)
    assert callable(auth.login_required)
    assert callable(auth.anonymous_required)
    assert callable(login_required)
    assert callable(anonymous_required)


@pytest.mark.anyio
async def test_wybra_auth_setup_site_allows_database_provider_later(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=_site_config_source(
            tmp_path,
            modules=("wybra.forms", "wybra.auth", "wybra.db"),
        ),
    )

    assert site.has_capability(DatabaseCapability) is True
    assert site.has_capability(AuthCapability) is True


@pytest.mark.anyio
async def test_wybra_auth_post_setup_site_requires_database_capability(
    tmp_path: Path,
) -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=_site_config_source(
                tmp_path,
                modules=("wybra.forms", "wybra.auth"),
            ),
        )


@pytest.mark.anyio
async def test_wybra_auth_setup_is_omitted_when_module_is_not_configured(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=_site_config_source(tmp_path, modules=("wybra.db",)),
    )

    assert site.has_capability(DatabaseCapability) is True
    assert site.has_capability(AuthCapability) is False
    assert all(route.path != "/account/login" for route in site.app.routes)


@pytest.mark.anyio
async def test_google_login_start_redirects_with_cookie_backed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/google/login?return_to=/dashboard",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    parsed_location = urlsplit(location)
    redirect_query = parse_qs(parsed_location.query)
    cookie_state = _google_oauth_cookie_state(site, response)
    assert parsed_location.scheme == "https"
    assert parsed_location.netloc == "accounts.google.com"
    assert parsed_location.path == "/o/oauth2/v2/auth"
    assert redirect_query["client_id"] == ["google-client-id"]
    assert redirect_query["redirect_uri"] == [
        "http://testserver/account/providers/google/callback"
    ]
    assert redirect_query["response_type"] == ["code"]
    assert redirect_query["scope"] == ["openid email profile"]
    assert redirect_query["state"] == [cookie_state.state]
    assert redirect_query["nonce"] == [cookie_state.nonce]
    assert cookie_state.provider_name == "google"
    assert cookie_state.purpose == "login"
    assert cookie_state.return_to == "/dashboard"
    assert cookie_state.redirect_uri == (
        "http://testserver/account/providers/google/callback"
    )
    assert cookie_state.user_id is None
    assert GOOGLE_OAUTH_STATE_COOKIE in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_login_start_uses_configured_provider_route_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        account_prefix="/identity",
        provider_route_prefix="/identity/oauth/google",
    )

    response = TestClient(site.app).get(
        "/identity/oauth/google/login",
        follow_redirects=False,
    )

    redirect_query = parse_qs(urlsplit(response.headers["location"]).query)
    cookie_state = _google_oauth_cookie_state(site, response)
    assert redirect_query["redirect_uri"] == [
        "http://testserver/identity/oauth/google/callback"
    ]
    assert cookie_state.return_to == "/identity"
    assert cookie_state.redirect_uri == (
        "http://testserver/identity/oauth/google/callback"
    )
    assert "/account/providers" not in redirect_query["redirect_uri"][0]


@pytest.mark.anyio
async def test_google_link_start_requires_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/google/link",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert GOOGLE_OAUTH_STATE_COOKIE not in response.cookies


@pytest.mark.anyio
async def test_google_link_start_records_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    user_id = uuid.uuid4()
    _override_current_user(site.app, user_id=user_id)

    response = TestClient(site.app).get(
        "/account/providers/google/link?return_to=/account/security",
        follow_redirects=False,
    )

    assert response.status_code == 303
    cookie_state = _google_oauth_cookie_state(site, response)
    assert cookie_state.purpose == "link"
    assert cookie_state.user_id == str(user_id)
    assert cookie_state.return_to == "/account/security"


@pytest.mark.anyio
async def test_google_start_routes_are_unavailable_when_provider_disabled(
    tmp_path: Path,
) -> None:
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(enabled=False),
    )

    response = TestClient(site.app).get(
        "/account/providers/google/login",
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert GOOGLE_OAUTH_STATE_COOKIE not in response.cookies


@pytest.mark.anyio
async def test_google_start_routes_are_unavailable_after_secret_degradation(
    tmp_path: Path,
) -> None:
    site = await _start_google_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/google/login",
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert GOOGLE_OAUTH_STATE_COOKIE not in response.cookies


@pytest.mark.anyio
async def test_google_callback_rejects_missing_state_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/google/callback?code=code&state=state",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google callback state is invalid."


@pytest.mark.anyio
async def test_google_callback_rejects_mismatched_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        "/account/providers/google/callback?code=code&state=other-state",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google callback state is invalid."
    assert cookie_state.state != "other-state"
    assert GOOGLE_OAUTH_STATE_COOKIE in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_rejects_expired_state_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    expired_state = create_google_oauth_state(
        purpose="login",
        return_to="/account",
        redirect_uri="http://testserver/account/providers/google/callback",
        now=1.0,
    )
    client = TestClient(site.app)
    client.cookies.set(
        GOOGLE_OAUTH_STATE_COOKIE,
        encode_google_oauth_state_cookie(
            expired_state,
            secret=_google_state_cookie_secret(site),
        ),
    )

    response = client.get(
        f"/account/providers/google/callback?code=code&state={expired_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google callback state is invalid."
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_exchanges_code_through_configured_token_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    token_client = FakeGoogleTokenClient()
    id_token_validator = FakeGoogleIDTokenValidator()
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        id_token_validator,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Google account is not linked."
    assert len(token_client.requests) == 1
    token_request = token_client.requests[0]
    assert token_request.token_endpoint == "https://oauth2.googleapis.com/token"
    assert token_request.client_id == "google-client-id"
    assert token_request.client_secret == "client-secret"
    assert token_request.code == "code"
    assert token_request.redirect_uri == (
        "http://testserver/account/providers/google/callback"
    )
    assert len(id_token_validator.requests) == 1
    validation_request = id_token_validator.requests[0]
    assert validation_request.id_token == "id-token"
    assert validation_request.settings.client_id == "google-client-id"
    assert validation_request.nonce == cookie_state.nonce
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_resolves_existing_provider_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="linked@example.com",
        is_verified=True,
    )
    await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-subject",
        account_email="linked@example.com",
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-subject",
                email="linked@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_google_callback_requires_local_totp_for_linked_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        providers_config=_google_provider_config(),
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="totp-linked@example.com",
        is_verified=True,
    )
    await _create_active_totp_credential(site, user_id)
    await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-totp-subject",
        account_email="totp-linked@example.com",
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-totp-subject",
                email="totp-linked@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Two-factor authentication" in response.text
    assert "Authenticator code" in response.text
    assert TOTP_LOGIN_NONCE_COOKIE in response.cookies
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_google_callback_auto_links_verified_email_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(email_match_linking_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="match@example.com",
        is_verified=True,
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
                refresh_token="refresh-token",
                expires_in=300,
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-match-subject",
                email="match@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-match-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_google_callback_email_match_verifies_matching_local_email(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(email_match_linking_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="match-unverified@example.com",
        is_verified=False,
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-match-unverified-subject",
                email="match-unverified@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    user = await _google_user_by_email(site, "match-unverified@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert user is not None
    assert user.id == user_id
    assert user.is_verified is True
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_google_callback_linked_login_verifies_matching_local_email(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="linked-unverified@example.com",
        is_verified=False,
    )
    await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-linked-unverified-subject",
        account_email="linked-unverified@example.com",
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-linked-unverified-subject",
                email="linked-unverified@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    user = await _google_user_by_email(site, "linked-unverified@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert user is not None
    assert user.is_verified is True
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_google_callback_creates_provider_account_with_password_login_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(account_creation_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-created-subject",
                email="created@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    created_user = await _google_user_by_email(site, "created@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert created_user is not None
    assert created_user.hashed_password is None
    assert created_user.password_login_enabled is False
    assert created_user.is_verified is True
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-created-subject",
        )
        == created_user.id
    )


@pytest.mark.anyio
async def test_google_callback_rejects_provider_token_storage_without_crypto_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(account_creation_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.from_key_bundle(None)
    await _create_auth_schema(site)
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-missing-crypto-subject",
                email="missing-crypto@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Google login is not available."
    assert await _google_user_by_email(site, "missing-crypto@example.com") is None
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-missing-crypto-subject",
        )
        is None
    )


@pytest.mark.anyio
async def test_google_callback_created_unverified_account_requires_email_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(account_creation_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-unverified-subject",
                email="unverified-provider@example.com",
                email_verified=False,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    created_user = await _google_user_by_email(
        site,
        "unverified-provider@example.com",
    )
    assert response.status_code == 403
    assert "Verify your email before signing in." in response.text
    assert created_user is not None
    assert created_user.is_verified is False
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_google_callback_links_provider_to_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-link-subject",
                email="current@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/google/link", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-link-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_google_callback_rejects_link_when_session_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    token_client = FakeGoogleTokenClient()
    setattr(site.app.state, GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE, token_client)
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/google/link", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)
    state_cookie = start.cookies.get(GOOGLE_OAUTH_STATE_COOKIE)
    assert state_cookie is not None
    client_without_session = TestClient(site.app)
    client_without_session.cookies.set(GOOGLE_OAUTH_STATE_COOKIE, state_cookie)

    response = client_without_session.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Google linking requires an active session."
    assert token_client.requests == []


@pytest.mark.anyio
async def test_google_callback_rejects_link_when_session_user_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    await _create_local_user(
        site,
        email="first@example.com",
        is_verified=True,
    )
    await _create_local_user(
        site,
        email="second@example.com",
        is_verified=True,
    )
    token_client = FakeGoogleTokenClient()
    setattr(site.app.state, GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE, token_client)
    client = _authenticated_client(site, email="first@example.com")
    start = client.get("/account/providers/google/link", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)
    login_page = client.get("/account/login")
    response = client.post(
        "/account/login",
        data={
            "csrf_token": _csrf_token(login_page.text),
            "email": "second@example.com",
            "password": STRONG_TEST_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Google linking requires an active session."
    assert token_client.requests == []


@pytest.mark.anyio
async def test_google_callback_allows_multiple_google_links_for_same_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-first-subject",
        account_email="first-google@example.com",
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(
            response=GoogleTokenResponse(
                access_token="access-token",
                id_token="id-token",
            )
        ),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-second-subject",
                email="second-google@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/google/link", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _google_provider_subjects_for_user(site, user_id=user_id) == (
        "google-first-subject",
        "google-second-subject",
    )


@pytest.mark.anyio
async def test_google_callback_rejects_explicit_link_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    linked_user_id = await _create_local_user(
        site,
        email="linked@example.com",
        is_verified=True,
    )
    await _create_google_provider_link(
        site,
        user_id=linked_user_id,
        provider_subject="google-subject",
        account_email="linked@example.com",
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(
            claims=GoogleIDTokenClaims(
                subject="google-subject",
                email="linked@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/google/link", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Google account is already linked to another user."
    )


@pytest.mark.anyio
async def test_google_callback_rejects_token_exchange_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    token_client = FakeGoogleTokenClient(
        error=GoogleTokenExchangeError("Google token exchange failed.")
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google token exchange failed."
    assert len(token_client.requests) == 1
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_rejects_missing_id_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    token_client = FakeGoogleTokenClient(response=GoogleTokenResponse())
    id_token_validator = FakeGoogleIDTokenValidator()
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        id_token_validator,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google ID token is invalid."
    assert len(token_client.requests) == 1
    assert id_token_validator.requests == []
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_rejects_invalid_id_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    token_client = FakeGoogleTokenClient()
    id_token_validator = FakeGoogleIDTokenValidator(
        error=GoogleIDTokenValidationError("Google ID token is invalid.")
    )
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        id_token_validator,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Google ID token is invalid."
    assert len(token_client.requests) == 1
    assert len(id_token_validator.requests) == 1
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_google_callback_state_cookie_is_cleared_after_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    setattr(
        site.app.state,
        GOOGLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGoogleTokenClient(),
    )
    setattr(
        site.app.state,
        GOOGLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeGoogleIDTokenValidator(),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/google/login", follow_redirects=False)
    cookie_state = _google_oauth_cookie_state(site, start)
    client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    replay = client.get(
        f"/account/providers/google/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert replay.status_code == 400
    assert replay.json()["detail"] == "Google callback state is invalid."


@pytest.mark.anyio
async def test_github_login_start_redirects_with_cookie_backed_pkce_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/github/login?return_to=/dashboard",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    parsed_location = urlsplit(location)
    redirect_query = parse_qs(parsed_location.query)
    cookie_state = _github_oauth_cookie_state(site, response)
    assert parsed_location.scheme == "https"
    assert parsed_location.netloc == "github.com"
    assert parsed_location.path == "/login/oauth/authorize"
    assert redirect_query["client_id"] == ["github-client-id"]
    assert redirect_query["redirect_uri"] == [
        "http://testserver/account/providers/github/callback"
    ]
    assert redirect_query["scope"] == ["read:user user:email"]
    assert redirect_query["state"] == [cookie_state.state]
    assert redirect_query["code_challenge"] == [cookie_state.code_challenge]
    assert redirect_query["code_challenge_method"] == ["S256"]
    assert "nonce" not in redirect_query
    assert cookie_state.provider_name == "github"
    assert cookie_state.purpose == "login"
    assert cookie_state.return_to == "/dashboard"
    assert cookie_state.redirect_uri == (
        "http://testserver/account/providers/github/callback"
    )
    assert cookie_state.user_id is None
    assert GITHUB_OAUTH_STATE_COOKIE in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_github_callback_exchanges_code_and_fetches_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    await _create_auth_schema(site)
    token_client = FakeGitHubTokenClient()
    api_client = FakeGitHubAPIClient()
    setattr(
        site.app.state,
        GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    setattr(site.app.state, GITHUB_API_CLIENT_STATE_ATTRIBUTE, api_client)
    client = TestClient(site.app)
    start = client.get("/account/providers/github/login", follow_redirects=False)
    cookie_state = _github_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/github/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "GitHub account is not linked."
    assert len(token_client.requests) == 1
    token_request = token_client.requests[0]
    assert token_request.token_endpoint == "https://github.com/login/oauth/access_token"
    assert token_request.client_id == "github-client-id"
    assert token_request.client_secret == "client-secret"
    assert token_request.code == "code"
    assert token_request.redirect_uri == (
        "http://testserver/account/providers/github/callback"
    )
    assert token_request.code_verifier == cookie_state.code_verifier
    assert len(api_client.requests) == 1
    identity_request = api_client.requests[0]
    assert identity_request.settings.client_id == "github-client-id"
    assert identity_request.access_token == "access-token"
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_github_callback_rejects_missing_required_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    token_client = FakeGitHubTokenClient(
        response=GitHubTokenResponse(
            access_token="access-token",
            token_type="bearer",
            scope="read:user",
        )
    )
    api_client = FakeGitHubAPIClient()
    setattr(site.app.state, GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE, token_client)
    setattr(site.app.state, GITHUB_API_CLIENT_STATE_ATTRIBUTE, api_client)
    client = TestClient(site.app)
    start = client.get("/account/providers/github/login", follow_redirects=False)
    cookie_state = _github_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/github/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "GitHub token response is invalid."
    assert len(token_client.requests) == 1
    assert api_client.requests == []
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_github_callback_resolves_existing_provider_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="linked@example.com",
        is_verified=True,
    )
    await _create_github_provider_link(
        site,
        user_id=user_id,
        provider_subject="github-subject",
        account_email="linked@example.com",
    )
    setattr(
        site.app.state,
        GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubTokenClient(),
    )
    setattr(
        site.app.state,
        GITHUB_API_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubAPIClient(
            claims=GitHubUserClaims(
                subject="github-subject",
                email="linked@example.com",
                email_verified=True,
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/github/login", follow_redirects=False)
    cookie_state = _github_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/github/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_github_callback_auto_links_verified_email_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(
        tmp_path,
        providers_config=_github_provider_config(email_match_linking_enabled=True),
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="match@example.com",
        is_verified=True,
    )
    setattr(
        site.app.state,
        GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubTokenClient(
            response=GitHubTokenResponse(
                access_token="access-token",
                token_type="bearer",
                scope="read:user,user:email",
                expires_in=300,
            )
        ),
    )
    setattr(
        site.app.state,
        GITHUB_API_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubAPIClient(
            claims=GitHubUserClaims(
                subject="github-match-subject",
                email="match@example.com",
                email_verified=True,
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/github/login", follow_redirects=False)
    cookie_state = _github_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/github/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies
    assert (
        await _github_provider_linked_user_id(
            site,
            provider_subject="github-match-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_github_callback_links_provider_to_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    setattr(
        site.app.state,
        GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubTokenClient(),
    )
    setattr(
        site.app.state,
        GITHUB_API_CLIENT_STATE_ATTRIBUTE,
        FakeGitHubAPIClient(
            claims=GitHubUserClaims(
                subject="github-link-subject",
                email="current@example.com",
                email_verified=True,
            )
        ),
    )
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/github/link", follow_redirects=False)
    cookie_state = _github_oauth_cookie_state(site, start)

    response = client.get(
        f"/account/providers/github/callback?code=code&state={cookie_state.state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert (
        await _github_provider_linked_user_id(
            site,
            provider_subject="github-link-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_login_page_shows_google_sign_in_when_provider_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)

    response = TestClient(site.app).get("/account/login?return_to=/dashboard")

    assert response.status_code == 200
    assert "Sign in with Google" in response.text
    assert "/account/providers/google/login?return_to=%2Fdashboard" in response.text


@pytest.mark.anyio
async def test_login_page_shows_github_sign_in_when_provider_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)

    response = TestClient(site.app).get("/account/login?return_to=/dashboard")

    assert response.status_code == 200
    assert "Sign in with GitHub" in response.text
    assert "/account/providers/github/login?return_to=%2Fdashboard" in response.text


@pytest.mark.anyio
async def test_login_page_hides_google_sign_in_when_provider_disabled(
    tmp_path: Path,
) -> None:
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(enabled=False),
    )

    response = TestClient(site.app).get("/account/login")

    assert response.status_code == 200
    assert "Sign in with Google" not in response.text


@pytest.mark.anyio
async def test_login_and_security_pages_hide_google_when_oauth_config_incomplete(
    tmp_path: Path,
) -> None:
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(secret_key=None),
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)

    login_response = client.get("/account/login")
    security_response = client.get("/account/security")

    assert login_response.status_code == 200
    assert "Sign in with Google" not in login_response.text
    assert security_response.status_code == 200
    assert "Provider sign-in" not in security_response.text


@pytest.mark.anyio
async def test_security_page_shows_google_link_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Provider sign-in" in response.text
    assert "Link Google" in response.text
    assert "/account/providers/google/link?return_to=%2Faccount%2Fsecurity" in (
        response.text
    )


@pytest.mark.anyio
async def test_security_page_shows_github_link_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Provider sign-in" in response.text
    assert "Link GitHub" in response.text
    assert "/account/providers/github/link?return_to=%2Faccount%2Fsecurity" in (
        response.text
    )


@pytest.mark.anyio
async def test_security_page_shows_google_unlink_and_password_disable_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    provider_id = await _create_google_provider_link(
        site,
        user_id=user_id,
        account_email="google-user@example.test",
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Google sign-in is linked as google-user@example.test" in response.text
    assert f'value="{provider_id}"' in response.text
    assert "Link another Google account" in response.text
    assert "Unlink Google" in response.text
    assert "Disable username/password login" in response.text


@pytest.mark.anyio
async def test_security_page_shows_github_unlink_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    provider_id = await _create_github_provider_link(
        site,
        user_id=user_id,
        account_email="github-user@example.test",
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "GitHub sign-in is linked as github-user@example.test" in response.text
    assert f'value="{provider_id}"' in response.text
    assert "Link another GitHub account" in response.text
    assert "Unlink GitHub" in response.text
    assert "Disable username/password login" in response.text


@pytest.mark.anyio
async def test_security_page_unlinks_google_when_password_login_remains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    provider_id = await _create_google_provider_link(site, user_id=user_id)
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/providers/google/unlink",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "provider_id": provider_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-subject",
        )
        is None
    )


@pytest.mark.anyio
async def test_security_page_rejects_unlinking_last_sign_in_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    provider_id = await _create_google_provider_link(site, user_id=user_id)
    _override_current_user(
        site.app,
        user_id=user_id,
        hashed_password=None,
        password_login_enabled=False,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/providers/google/unlink",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "provider_id": provider_id,
        },
    )

    assert response.status_code == 400
    assert "Add another sign-in method before unlinking Google." in response.text
    assert (
        await _google_provider_linked_user_id(
            site,
            provider_subject="google-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_security_page_rejects_unlinking_last_github_sign_in_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SECRET", "client-secret")
    site = await _start_github_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    provider_id = await _create_github_provider_link(site, user_id=user_id)
    _override_current_user(
        site.app,
        user_id=user_id,
        hashed_password=None,
        password_login_enabled=False,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/providers/github/unlink",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "provider_id": provider_id,
        },
    )

    assert response.status_code == 400
    assert "Add another sign-in method before unlinking GitHub." in response.text
    assert (
        await _github_provider_linked_user_id(
            site,
            provider_subject="github-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_security_page_unlinks_one_google_account_when_another_remains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    first_provider_id = await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-first-subject",
        account_email="first-google@example.com",
    )
    await _create_google_provider_link(
        site,
        user_id=user_id,
        provider_subject="google-second-subject",
        account_email="second-google@example.com",
    )
    _override_current_user(
        site.app,
        user_id=user_id,
        hashed_password=None,
        password_login_enabled=False,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/providers/google/unlink",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "provider_id": first_provider_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _google_provider_subjects_for_user(site, user_id=user_id) == (
        "google-second-subject",
    )


@pytest.mark.anyio
async def test_security_page_disables_password_login_when_google_is_linked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _create_google_provider_link(site, user_id=user_id)
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/password/disable",
        data={"csrf_token": _csrf_token(security_page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _password_login_enabled(site, user_id) is False


@pytest.mark.anyio
async def test_security_page_rejects_disabling_password_without_provider_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    login_page = client.get("/account/login")

    response = client.post(
        "/account/security/password/disable",
        data={"csrf_token": _csrf_token(login_page.text)},
    )

    assert response.status_code == 400
    assert "Add another sign-in method before disabling password sign-in." in (
        response.text
    )
    assert await _password_login_enabled(site, user_id) is True


@pytest.mark.anyio
async def test_security_page_rejects_disabling_password_when_google_unavailable(
    tmp_path: Path,
) -> None:
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(enabled=False),
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _create_google_provider_link(site, user_id=user_id)
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    login_page = client.get("/account/login")

    response = client.post(
        "/account/security/password/disable",
        data={"csrf_token": _csrf_token(login_page.text)},
    )

    assert response.status_code == 400
    assert "Add another sign-in method before disabling password sign-in." in (
        response.text
    )
    assert await _password_login_enabled(site, user_id) is True


@pytest.mark.anyio
async def test_security_page_requires_authenticated_user(tmp_path: Path) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=_site_config_source(
            tmp_path,
            modules=PAGE_MODULES,
        ),
    )

    response = TestClient(site.app, raise_server_exceptions=False).get(
        "/account/security"
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_login_requires_verified_email_before_session_issue(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=_site_config_source(
            tmp_path,
            modules=PAGE_MODULES,
        ),
    )
    await _create_auth_schema(site)
    await _create_local_user(
        site,
        email="unverified@example.com",
        is_verified=False,
    )

    client = TestClient(site.app)
    login_page = client.get("/account/login")
    response = client.post(
        "/account/login",
        data={
            "csrf_token": _csrf_token(login_page.text),
            "email": "unverified@example.com",
            "password": STRONG_TEST_PASSWORD,
        },
    )

    assert response.status_code == 403
    assert "Verify your email before signing in." in response.text
    assert 'value="unverified@example.com"' in response.text
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_login_rejects_disabled_password_login(tmp_path: Path) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=_site_config_source(
            tmp_path,
            modules=PAGE_MODULES,
        ),
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="password-disabled@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)

    client = TestClient(site.app)
    login_page = client.get("/account/login")
    response = client.post(
        "/account/login",
        data={
            "csrf_token": _csrf_token(login_page.text),
            "email": "password-disabled@example.com",
            "password": STRONG_TEST_PASSWORD,
        },
    )

    assert response.status_code == 401
    assert "Email or password is incorrect." in response.text
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_security_page_renders_for_authenticated_user(tmp_path: Path) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=_site_config_source(
            tmp_path,
            modules=PAGE_MODULES,
        ),
    )

    _override_current_user(site.app)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Login &amp; Security" in response.text
    assert "security@example.test" in response.text


@pytest.mark.anyio
async def test_security_page_omits_totp_section_when_totp_disabled(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(tmp_path)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Authenticator app" not in response.text


@pytest.mark.anyio
async def test_security_page_shows_totp_setup_when_totp_enabled_without_credential(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(tmp_path, auth_config={"totp_mode": "opt_in"})
    await _create_auth_schema(site)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Authenticator app" in response.text
    assert "Set up authenticator" in response.text
    assert "/account/totp/setup?return_to=%2Faccount%2Fsecurity" in response.text


@pytest.mark.anyio
async def test_security_page_totp_setup_link_uses_configured_account_prefix(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        account_prefix="/identity",
        auth_config={"totp_mode": "opt_in"},
    )
    await _create_auth_schema(site)

    response = _security_page_client(site).get("/identity/security")

    assert response.status_code == 200
    assert "/identity/totp/setup?return_to=%2Fidentity%2Fsecurity" in response.text
    assert "/account/security" not in response.text


@pytest.mark.anyio
async def test_totp_setup_page_shows_setup_qr_code_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        expires_at=None,
    )

    async def current_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "resolve_current_user",
        current_user,
    )

    response = _security_page_client(site).get("/account/totp/setup")

    assert response.status_code == 200
    assert "Show setup QRCode" in response.text
    assert "<svg" in response.text
    assert "otpauth://totp/" not in response.text.split("<summary>Show setup URI")[0]
    assert "Show setup URI" in response.text


@pytest.mark.anyio
async def test_totp_setup_completion_returns_to_security_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        expires_at=None,
    )

    async def current_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "resolve_current_user",
        current_user,
    )
    client = _security_page_client(site)
    setup = client.get("/account/totp/setup?return_to=/account/security?tab=totp")
    secret_match = re.search(
        r"<strong>Secret:</strong> <code>([^<]+)</code>",
        setup.text,
    )
    assert secret_match is not None

    response = client.post(
        "/account/totp/setup",
        data={
            "csrf_token": _csrf_token(setup.text),
            "return_to": "/account/security?tab=totp",
            "setup_challenge_id": "",
            "setup_totp_code": generate_totp(secret_match.group(1)),
        },
    )

    assert response.status_code == 200
    assert "Store these recovery codes" in response.text
    assert "Return to Login &amp; Security" in response.text
    assert 'href="/account/security?tab=totp"' in response.text
    _assert_recovery_codes_download(response.text)


@pytest.mark.anyio
async def test_security_page_shows_totp_controls_when_totp_is_active(
    tmp_path: Path,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    await _create_active_totp_credential(site, user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Authenticator verification is enabled" in response.text
    assert "Disable authenticator" in response.text
    assert "/account/totp/disable" in response.text


@pytest.mark.anyio
async def test_totp_disable_requires_confirmation_before_disabling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={"csrf_token": _csrf_token(confirmation.text)},
    )

    assert response.status_code == 400
    assert "Confirm this action" in response.text
    assert await _active_totp_credential_id(site, user_id) is not None


@pytest.mark.anyio
async def test_totp_disable_accepts_active_totp_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "totp_code": generate_totp(secret),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _active_totp_credential_id(site, user_id) is None


@pytest.mark.anyio
async def test_totp_disable_accepts_password_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    async def authenticate_user(_request, email: str, password: str):
        return user if email == user.email and password == "correct-password" else None

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    monkeypatch.setattr(
        "wybra.auth.routes.pages.shared.authenticate_user",
        authenticate_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "password": "correct-password",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _active_totp_credential_id(site, user_id) is None


@pytest.mark.anyio
async def test_totp_disable_accepts_recovery_code_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    _secret, recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "recovery_code": recovery_codes[0],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _active_totp_credential_id(site, user_id) is None


@pytest.mark.anyio
async def test_totp_disable_rejects_removing_last_usable_sign_in_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password=None,
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "totp_code": generate_totp(secret),
        },
    )

    assert response.status_code == 400
    assert "Add another sign-in method" in response.text
    assert await _active_totp_credential_id(site, user_id) is not None


@pytest.mark.anyio
async def test_totp_disable_allows_linked_google_as_remaining_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "client-secret")
    site = await _start_google_provider_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)
    await _create_google_provider_link(site, user_id=user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password=None,
        password_login_enabled=False,
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "totp_code": generate_totp(secret),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _active_totp_credential_id(site, user_id) is None


@pytest.mark.anyio
async def test_totp_disable_rejects_unavailable_google_as_remaining_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = await _start_google_provider_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        providers_config=_google_provider_config(enabled=False),
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)
    await _create_google_provider_link(site, user_id=user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password=None,
        password_login_enabled=False,
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/disable")

    response = client.post(
        "/account/totp/disable",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "totp_code": generate_totp(secret),
        },
    )

    assert response.status_code == 400
    assert "Add another sign-in method" in response.text
    assert await _active_totp_credential_id(site, user_id) is not None


@pytest.mark.anyio
async def test_security_page_links_to_recovery_code_replacement_when_totp_is_active(
    tmp_path: Path,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    await _create_active_totp_credential(site, user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Generate replacement recovery codes" in response.text
    assert "/account/totp/recovery-codes/regenerate" in response.text


@pytest.mark.anyio
async def test_totp_recovery_code_replacement_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    _secret, recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/recovery-codes/regenerate")

    assert confirmation.status_code == 200
    assert "Confirm this action with your password" in confirmation.text
    assert "one of your existing sign-in methods" not in confirmation.text
    assert 'name="confirmation"' in confirmation.text
    assert 'name="password"' not in confirmation.text
    assert 'name="totp_code"' not in confirmation.text
    assert 'name="recovery_code"' not in confirmation.text

    response = client.post(
        "/account/totp/recovery-codes/regenerate",
        data={"csrf_token": _csrf_token(confirmation.text)},
    )

    assert response.status_code == 400
    assert "Confirm this action" in response.text
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        recovery_store = SqlAlchemyRecoveryCodeStore(
            db_session,
            site.app.state.secret_envelope_service,
        )
        assert await recovery_store.consume_recovery_code(
            str(user_id),
            recovery_codes[0],
        )


@pytest.mark.anyio
async def test_totp_recovery_code_replacement_rotates_codes_after_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    _secret, recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/recovery-codes/regenerate")

    response = client.post(
        "/account/totp/recovery-codes/regenerate",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "confirmation": recovery_codes[0],
        },
    )

    assert response.status_code == 200
    assert "Store these recovery codes" in response.text
    _assert_recovery_codes_download(response.text)
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        recovery_store = SqlAlchemyRecoveryCodeStore(
            db_session,
            site.app.state.secret_envelope_service,
        )
        assert not await recovery_store.consume_recovery_code(
            str(user_id),
            recovery_codes[0],
        )


@pytest.mark.anyio
async def test_totp_recovery_code_replacement_accepts_single_totp_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/recovery-codes/regenerate")

    response = client.post(
        "/account/totp/recovery-codes/regenerate",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "confirmation": generate_totp(secret),
        },
    )

    assert response.status_code == 200
    assert "Store these recovery codes" in response.text


@pytest.mark.anyio
async def test_totp_recovery_code_replacement_accepts_single_password_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    site = await _start_security_site(
        tmp_path,
        auth_config={"totp_mode": "opt_in"},
        user_id=user_id,
    )
    await _create_auth_schema(site)
    await _create_active_totp_credential(site, user_id)

    user = SimpleNamespace(
        id=user_id,
        email="security@example.test",
        is_active=True,
        is_verified=True,
        hashed_password="hash",
    )

    async def authenticated_user(_request):
        return user

    async def authenticate_user(_request, email: str, password: str):
        return user if email == user.email and password == "correct-password" else None

    monkeypatch.setattr(
        totp_management_pages,
        "_require_authenticated_user",
        authenticated_user,
    )
    monkeypatch.setattr(
        "wybra.auth.routes.pages.shared.authenticate_user",
        authenticate_user,
    )
    client = _security_page_client(site)
    confirmation = client.get("/account/totp/recovery-codes/regenerate")

    response = client.post(
        "/account/totp/recovery-codes/regenerate",
        data={
            "csrf_token": _csrf_token(confirmation.text),
            "confirmation": "correct-password",
        },
    )

    assert response.status_code == 200
    assert "Store these recovery codes" in response.text
