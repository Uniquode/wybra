import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient

from provider_test_keys import apple_private_key_pem as _apple_private_key_pem
from support_database import sqlite_file_url
from wybra.auth import (
    AuthCapability,
    AuthPersistenceCapability,
    anonymous_required,
    login_required,
)
from wybra.auth.accounts.manager import create_user_manager
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.delivery import NullIdentityDelivery
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    TortoiseChallengeStore,
    TortoiseRecoveryCodeStore,
    TortoiseTOTPCredentialStore,
    TortoiseWebAuthnCredentialStore,
)
from wybra.auth.mfa.totp import generate_totp, generate_totp_secret
from wybra.auth.mfa.webauthn import credential_id_to_text
from wybra.auth.models import ExternalIdentityLink, IdentityProvider, User
from wybra.auth.provider_credentials import TortoiseProviderCredentialStore
from wybra.auth.routes.pages import passkeys as passkey_pages
from wybra.auth.routes.pages import totp_management as totp_management_pages
from wybra.auth.routes.totp import TOTP_LOGIN_NONCE_COOKIE
from wybra.config import MappingConfigSource
from wybra.db import DatabaseCapability, TortoiseDatabaseCapability
from wybra.providers.apple import (
    APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
    APPLE_OAUTH_STATE_COOKIE,
    APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    APPLE_PROVIDER_NAME,
    AppleIDTokenClaims,
    AppleIDTokenValidationError,
    AppleIDTokenValidationRequest,
    AppleOAuthState,
    AppleTokenExchangeError,
    AppleTokenExchangeRequest,
    AppleTokenResponse,
    decode_apple_oauth_state_cookie,
)
from wybra.providers.descriptors import provider_label
from wybra.providers.github import (
    GITHUB_API_CLIENT_STATE_ATTRIBUTE,
    GITHUB_OAUTH_STATE_COOKIE,
    GITHUB_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
    GITHUB_PROVIDER_NAME,
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
    GOOGLE_PROVIDER_NAME,
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
from wybra.services.secrets import MissingSecretError, SecretsCapability, SecretValue
from wybra.site import Site, SiteCapabilityError, start


class TestClient(FastAPITestClient):
    """Test client that closes Tortoise connections before request-loop teardown."""

    @contextmanager
    def _portal_factory(self):
        with super()._portal_factory() as portal:
            try:
                yield portal
            finally:
                portal.call(_close_testclient_tortoise_connections, self.app)


async def _close_testclient_tortoise_connections(app) -> None:
    site = getattr(getattr(app, "state", None), "site", None)
    if site is None:
        return
    try:
        database = site.require_capability(DatabaseCapability)
    except SiteCapabilityError:
        return
    if not isinstance(database, TortoiseDatabaseCapability):
        return

    # TestClient runs requests on a private loop. Close any connections opened
    # there before that loop is torn down; the next request or assertion can
    # reconnect through the same Tortoise context.
    with database._database.context:
        await database._database.context.connections.close_all(discard=True)


# These async tests intentionally exercise the ASGI app through Starlette's
# synchronous TestClient. TestClient runs requests on its own event loop, while
# setup/assertion helpers use pytest's anyio loop. Runtime ASGI servers do not
# use this split, and Tortoise reconnects safely in this test-only pattern.
pytestmark = pytest.mark.filterwarnings(
    "ignore::tortoise.warnings.TortoiseLoopSwitchWarning"
)

PAGE_MODULES = (
    "wybra.forms",
    "wybra.assets",
    "wybra.template",
    "wybra.db",
    "wybra.auth",
)
STRONG_TEST_PASSWORD = "Correct horse 42!"
PASSKEY_AUTH_CONFIG = {
    "passkey_enabled": True,
    "passkeys": {
        "rp_id": "testserver",
        "rp_name": "Test App",
        "allowed_origins": ["http://testserver"],
        "timeout_seconds": 300,
        "user_verification": "preferred",
        "attestation": "none",
        "discoverable_credentials": "preferred",
        "counter_policy": "reject-regression",
    },
}


class MissingSecretsCapability:
    def resolve(self, source: str, key: str) -> SecretValue:
        raise MissingSecretError(source=source, key=key)

    def exists(self, source: str, key: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ProviderPageCase:
    name: str
    label: str
    state_cookie_name: str
    default_subject: str
    available_secret_name: str
    available_secret_value: Callable[[], str]
    start_site: Callable[..., Awaitable[Site]]
    config_factory: Callable[..., dict[str, object]]
    create_link: Callable[..., Awaitable[str]]
    linked_user_id: Callable[..., Awaitable[uuid.UUID | None]]

    def page_params(self) -> tuple[str, str]:
        return self.name, self.label

    def set_available_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self.available_secret_name, self.available_secret_value())


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
        route_prefixes["wybra.providers"] = {
            GOOGLE_PROVIDER_NAME: provider_route_prefix
        }

    config: dict[str, object] = {
        "app": {
            "config_path": tmp_path / "app.toml",
            "project_root": tmp_path,
            "modules": modules,
            "database_url": sqlite_file_url(tmp_path / "app.sqlite3"),
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
    database = site.require_capability(DatabaseCapability)
    assert isinstance(database, TortoiseDatabaseCapability)
    await database.generate_schemas()
    user_id = getattr(site.app.state, "security_test_user_id", None)
    if isinstance(user_id, uuid.UUID):
        await _ensure_identity_user(site, user_id)


async def _create_active_totp_credential(
    site,
    user_id: uuid.UUID,
) -> tuple[str, tuple[str, ...]]:
    secret_service = SecretEnvelopeService.for_testing()
    site.app.state.secret_envelope_service = secret_service
    secret = generate_totp_secret()
    recovery_codes = generate_recovery_codes()
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = TortoiseTOTPCredentialStore(
            db_session,
            secret_service,
        )
        credential_id = await store.create_pending_totp_credential(
            str(user_id),
            secret,
        )
        await store.activate_totp_credential(credential_id)
        recovery_store = TortoiseRecoveryCodeStore(db_session, secret_service)
        await recovery_store.replace_recovery_codes(
            str(user_id),
            credential_id,
            recovery_codes,
        )
    return secret, recovery_codes


async def _ensure_identity_user(
    site,
    user_id: uuid.UUID,
    *,
    email: str = "security@example.test",
) -> None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        await User.get_or_create(
            id=user_id,
            defaults={
                "email": email,
                "hashed_password": "hash",
                "is_active": True,
                "is_verified": True,
            },
            using_db=db_session,
        )


async def _active_totp_credential_id(site, user_id: uuid.UUID) -> str | None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = TortoiseTOTPCredentialStore(
            db_session,
            site.app.state.secret_envelope_service,
        )
        return await store.get_active_totp_credential(str(user_id))


async def _create_passkey_credential(
    site,
    *,
    user_id: uuid.UUID,
    credential_id: str | None = None,
    label: str = "Test passkey",
) -> str:
    stored_credential_id = credential_id or credential_id_to_text(b"test-passkey")
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = TortoiseWebAuthnCredentialStore(db_session)
        return await store.store_webauthn_credential(
            str(user_id),
            stored_credential_id,
            b"public-key",
            0,
            label=label,
            user_verified=True,
            credential_device_type="multi_device",
            credential_backed_up=True,
            transports=("internal",),
            aaguid="test-aaguid",
            attestation_format="none",
        )


async def _active_passkey_count(site, user_id: uuid.UUID) -> int:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = TortoiseWebAuthnCredentialStore(db_session)
        return await store.count_active_webauthn_credentials(str(user_id))


async def _authentication_challenge_exists(site, challenge_id: str) -> bool:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        store = TortoiseChallengeStore(db_session)
        return await store.get_challenge(challenge_id) is not None


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


def _csrf_header(response_text: str) -> dict[str, str]:
    return {"x-csrf-token": _csrf_token(response_text)}


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
        await user.save(using_db=db_session)
        return user.id


async def _set_password_login_enabled(
    site,
    user_id: uuid.UUID,
    enabled: bool,
) -> None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        user = await User.get_or_none(id=user_id, using_db=db_session)
        assert user is not None
        user.password_login_enabled = enabled
        await user.save(using_db=db_session)


async def _password_login_enabled(site, user_id: uuid.UUID) -> bool:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:
        user = await User.get_or_none(id=user_id, using_db=db_session)
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
    resolved_user_id = user_id or uuid.uuid4()
    site = await start(app, config_source=config_source)
    site.app.state.security_test_user_id = resolved_user_id
    _override_current_user(site.app, user_id=resolved_user_id)
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
    return {GOOGLE_PROVIDER_NAME: config}


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
    return {GITHUB_PROVIDER_NAME: config}


def _apple_provider_config(
    *,
    enabled: bool = True,
    secret_key: str | None = "APPLE_PRIVATE_KEY",
    account_creation_enabled: bool = False,
    email_match_linking_enabled: bool = False,
) -> dict[str, object]:
    config: dict[str, object] = {
        "enabled": enabled,
        "client_id": "com.example.app.web",
        "team_id": "TEAMID1234",
        "key_id": "KEYID1234",
        "account_creation_enabled": account_creation_enabled,
        "email_match_linking_enabled": email_match_linking_enabled,
        "required_claims": ["sub", "email", "email_verified"],
    }
    if secret_key is not None:
        config.update(
            {
                "secrets": "environment",
                "private_key_secret_key": secret_key,
            }
        )
    return {APPLE_PROVIDER_NAME: config}


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
            or f"{account_prefix}/providers/{GOOGLE_PROVIDER_NAME}",
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
                GITHUB_PROVIDER_NAME: provider_route_prefix
                or f"{account_prefix}/providers/{GITHUB_PROVIDER_NAME}",
            },
            providers_config=providers_config or _github_provider_config(),
        ),
    )


async def _start_apple_provider_site(
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
                APPLE_PROVIDER_NAME: provider_route_prefix
                or f"{account_prefix}/providers/{APPLE_PROVIDER_NAME}",
            },
            providers_config=providers_config or _apple_provider_config(),
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


def _apple_oauth_cookie_state(site, response) -> AppleOAuthState:
    cookie = response.cookies.get(APPLE_OAUTH_STATE_COOKIE)
    assert cookie is not None
    state = decode_apple_oauth_state_cookie(
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
        store = TortoiseProviderCredentialStore(
            session,
            SecretEnvelopeService.for_testing(),
        )
        provider_id = await store.create_provider_credential(
            provider_name=GOOGLE_PROVIDER_NAME,
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
        store = TortoiseProviderCredentialStore(
            session,
            SecretEnvelopeService.for_testing(),
        )
        provider_id = await store.create_provider_credential(
            provider_name=GITHUB_PROVIDER_NAME,
            provider_subject=provider_subject,
            access_token="stored-access-token",
            account_email=account_email,
        )
        await store.link_provider_to_user(
            provider_id=provider_id,
            user_id=user_id,
        )
        return provider_id


async def _create_apple_provider_link(
    site,
    *,
    user_id: uuid.UUID,
    provider_subject: str = "apple-subject",
    account_email: str = "user@example.com",
) -> str:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        store = TortoiseProviderCredentialStore(
            session,
            SecretEnvelopeService.for_testing(),
        )
        provider_id = await store.create_provider_credential(
            provider_name=APPLE_PROVIDER_NAME,
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
        provider = await IdentityProvider.get_or_none(
            provider_name=GOOGLE_PROVIDER_NAME,
            provider_subject=provider_subject,
            using_db=session,
        )
        if provider is None:
            return None
        link = await ExternalIdentityLink.get_or_none(
            provider_id=provider.id,
            using_db=session,
        )
        return None if link is None else link.user_id


async def _github_provider_linked_user_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        provider = await IdentityProvider.get_or_none(
            provider_name=GITHUB_PROVIDER_NAME,
            provider_subject=provider_subject,
            using_db=session,
        )
        if provider is None:
            return None
        link = await ExternalIdentityLink.get_or_none(
            provider_id=provider.id,
            using_db=session,
        )
        return None if link is None else link.user_id


async def _apple_provider_linked_user_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        provider = await IdentityProvider.get_or_none(
            provider_name=APPLE_PROVIDER_NAME,
            provider_subject=provider_subject,
            using_db=session,
        )
        if provider is None:
            return None
        link = await ExternalIdentityLink.get_or_none(
            provider_id=provider.id,
            using_db=session,
        )
        return None if link is None else link.user_id


def _test_client_secret() -> str:
    return "client-secret"


PROVIDER_TEST_CASES = (
    ProviderPageCase(
        name=GOOGLE_PROVIDER_NAME,
        label=provider_label(GOOGLE_PROVIDER_NAME),
        state_cookie_name=GOOGLE_OAUTH_STATE_COOKIE,
        default_subject="google-subject",
        available_secret_name="GOOGLE_SECRET",
        available_secret_value=_test_client_secret,
        start_site=_start_google_provider_site,
        config_factory=_google_provider_config,
        create_link=_create_google_provider_link,
        linked_user_id=_google_provider_linked_user_id,
    ),
    ProviderPageCase(
        name=GITHUB_PROVIDER_NAME,
        label=provider_label(GITHUB_PROVIDER_NAME),
        state_cookie_name=GITHUB_OAUTH_STATE_COOKIE,
        default_subject="github-subject",
        available_secret_name="GITHUB_SECRET",
        available_secret_value=_test_client_secret,
        start_site=_start_github_provider_site,
        config_factory=_github_provider_config,
        create_link=_create_github_provider_link,
        linked_user_id=_github_provider_linked_user_id,
    ),
    ProviderPageCase(
        name=APPLE_PROVIDER_NAME,
        label=provider_label(APPLE_PROVIDER_NAME),
        state_cookie_name=APPLE_OAUTH_STATE_COOKIE,
        default_subject="apple-subject",
        available_secret_name="APPLE_PRIVATE_KEY",
        available_secret_value=_apple_private_key_pem,
        start_site=_start_apple_provider_site,
        config_factory=_apple_provider_config,
        create_link=_create_apple_provider_link,
        linked_user_id=_apple_provider_linked_user_id,
    ),
)
PROVIDER_PAGE_CASES = tuple(case.page_params() for case in PROVIDER_TEST_CASES)
_PROVIDER_TEST_CASE_BY_NAME = {case.name: case for case in PROVIDER_TEST_CASES}


def _provider_case(provider_name: str) -> ProviderPageCase:
    try:
        return _PROVIDER_TEST_CASE_BY_NAME[provider_name]
    except KeyError as exc:
        raise AssertionError(f"unknown provider: {provider_name}") from exc


def _set_available_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    _provider_case(provider_name).set_available_secret(monkeypatch)


async def _start_provider_site(
    provider_name: str,
    tmp_path: Path,
    *,
    providers_config: dict[str, object] | None = None,
):
    return await _provider_case(provider_name).start_site(
        tmp_path,
        providers_config=providers_config,
    )


def _provider_config(
    provider_name: str,
    **kwargs,
) -> dict[str, object]:
    return _provider_case(provider_name).config_factory(**kwargs)


async def _create_provider_link(
    provider_name: str,
    site,
    *,
    user_id: uuid.UUID,
    account_email: str = "user@example.com",
) -> str:
    return await _provider_case(provider_name).create_link(
        site,
        user_id=user_id,
        account_email=account_email,
    )


async def _provider_linked_user_id(
    provider_name: str,
    site,
) -> uuid.UUID | None:
    provider_case = _provider_case(provider_name)
    return await provider_case.linked_user_id(
        site,
        provider_subject=provider_case.default_subject,
    )


def _provider_state_cookie_name(provider_name: str) -> str:
    return _provider_case(provider_name).state_cookie_name


async def _google_provider_id(
    site,
    *,
    provider_subject: str,
) -> uuid.UUID | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        provider = await IdentityProvider.get_or_none(
            provider_name=GOOGLE_PROVIDER_NAME,
            provider_subject=provider_subject,
            using_db=session,
        )
        return None if provider is None else provider.id


async def _google_provider_subjects_for_user(
    site,
    *,
    user_id: uuid.UUID,
) -> tuple[str, ...]:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        provider_ids = (
            await ExternalIdentityLink.filter(
                user_id=user_id,
            )
            .using_db(session)
            .values_list("provider_id", flat=True)
        )
        providers = (
            await IdentityProvider.filter(
                id__in=provider_ids,
                provider_name=GOOGLE_PROVIDER_NAME,
            )
            .using_db(session)
            .order_by("provider_subject")
        )
        return tuple(provider.provider_subject for provider in providers)


async def _google_user_by_email(site, email: str) -> User | None:
    async with site.require_capability(DatabaseCapability).transaction() as session:
        return await User.get_or_none(email=email, using_db=session)


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


class FakeAppleTokenClient:
    def __init__(
        self,
        response: AppleTokenResponse | None = None,
        error: AppleTokenExchangeError | None = None,
    ) -> None:
        self.response = response or AppleTokenResponse(
            access_token="access-token",
            id_token="id-token",
            token_type="bearer",
        )
        self.error = error
        self.requests: list[AppleTokenExchangeRequest] = []

    async def exchange_code(
        self,
        request: AppleTokenExchangeRequest,
    ) -> AppleTokenResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAppleIDTokenValidator:
    def __init__(
        self,
        claims: AppleIDTokenClaims | None = None,
        error: AppleIDTokenValidationError | None = None,
    ) -> None:
        self.claims = claims or AppleIDTokenClaims(
            subject="apple-subject",
            email="user@example.com",
            email_verified=True,
            nonce="nonce",
            claims={
                "sub": "apple-subject",
                "email": "user@example.com",
                "email_verified": True,
            },
        )
        self.error = error
        self.requests: list[AppleIDTokenValidationRequest] = []

    async def validate(
        self,
        request: AppleIDTokenValidationRequest,
    ) -> AppleIDTokenClaims:
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
    auth_persistence = site.require_capability(AuthPersistenceCapability)

    assert site.has_capability(AuthCapability) is True
    assert site.has_capability(AuthPersistenceCapability) is True
    assert isinstance(auth, AuthCapability)
    assert isinstance(auth_persistence, AuthPersistenceCapability)
    assert auth.settings is app.state.auth_settings
    assert not hasattr(auth, "fastapi_users")
    assert not hasattr(app.state, "fastapi_users")
    assert isinstance(app.state.identity_delivery, NullIdentityDelivery)
    assert callable(auth.optional_current_user)
    assert callable(auth.login_required)
    assert callable(auth.anonymous_required)
    assert callable(auth_persistence.scope)
    assert callable(auth_persistence.transaction)
    async with auth_persistence.scope() as persistence_scope:
        assert callable(persistence_scope.management.resolve_user_record)
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
    assert cookie_state.provider_name == GOOGLE_PROVIDER_NAME
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
@pytest.mark.parametrize(
    "provider_name",
    (GOOGLE_PROVIDER_NAME, GITHUB_PROVIDER_NAME, APPLE_PROVIDER_NAME),
)
async def test_provider_start_routes_are_unavailable_after_secret_degradation(
    tmp_path: Path,
    provider_name: str,
) -> None:
    site = await _start_provider_site(tmp_path=tmp_path, provider_name=provider_name)

    response = TestClient(site.app).get(
        f"/account/providers/{provider_name}/login",
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert _provider_state_cookie_name(provider_name) not in response.cookies


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
    assert cookie_state.provider_name == GITHUB_PROVIDER_NAME
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
async def test_apple_login_start_redirects_with_cookie_backed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/apple/login?return_to=/dashboard",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    parsed_location = urlsplit(location)
    redirect_query = parse_qs(parsed_location.query)
    cookie_state = _apple_oauth_cookie_state(site, response)
    assert parsed_location.scheme == "https"
    assert parsed_location.netloc == "appleid.apple.com"
    assert parsed_location.path == "/auth/authorize"
    assert redirect_query["client_id"] == ["com.example.app.web"]
    assert redirect_query["redirect_uri"] == [
        "http://testserver/account/providers/apple/callback"
    ]
    assert redirect_query["response_type"] == ["code"]
    assert redirect_query["response_mode"] == ["form_post"]
    assert redirect_query["scope"] == ["name email"]
    assert redirect_query["state"] == [cookie_state.state]
    assert redirect_query["nonce"] == [cookie_state.nonce]
    assert cookie_state.provider_name == APPLE_PROVIDER_NAME
    assert cookie_state.purpose == "login"
    assert cookie_state.return_to == "/dashboard"
    assert cookie_state.redirect_uri == (
        "http://testserver/account/providers/apple/callback"
    )
    assert cookie_state.user_id is None
    assert APPLE_OAUTH_STATE_COOKIE in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_apple_start_routes_are_unavailable_after_private_key_degradation(
    tmp_path: Path,
) -> None:
    site = await _start_apple_provider_site(tmp_path)

    response = TestClient(site.app).get(
        "/account/providers/apple/login",
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert APPLE_OAUTH_STATE_COOKIE not in response.cookies


@pytest.mark.anyio
async def test_apple_callback_exchanges_code_and_validates_id_token_from_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    await _create_auth_schema(site)
    token_client = FakeAppleTokenClient()
    id_token_validator = FakeAppleIDTokenValidator()
    setattr(
        site.app.state,
        APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        token_client,
    )
    setattr(
        site.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        id_token_validator,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Apple account is not linked."
    assert len(token_client.requests) == 1
    token_request = token_client.requests[0]
    assert token_request.token_endpoint == "https://appleid.apple.com/auth/token"
    assert token_request.client_id == "com.example.app.web"
    assert token_request.client_secret.count(".") == 2
    assert token_request.code == "code"
    assert token_request.redirect_uri == (
        "http://testserver/account/providers/apple/callback"
    )
    assert len(id_token_validator.requests) == 1
    validation_request = id_token_validator.requests[0]
    assert validation_request.id_token == "id-token"
    assert validation_request.settings.client_id == "com.example.app.web"
    assert validation_request.nonce == cookie_state.nonce
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_apple_callback_rejects_invalid_token_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    token_client = FakeAppleTokenClient(response=AppleTokenResponse())
    id_token_validator = FakeAppleIDTokenValidator()
    setattr(site.app.state, APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE, token_client)
    setattr(
        site.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        id_token_validator,
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Apple token response is invalid."
    assert len(token_client.requests) == 1
    assert id_token_validator.requests == []
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_apple_callback_logs_private_key_resolution_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)
    site._capabilities[SecretsCapability] = MissingSecretsCapability()

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )
    captured = capsys.readouterr()

    assert response.status_code == 404
    assert response.json()["detail"] == "Apple login is not available."
    assert (
        "Apple private key resolution failed: source=environment key=APPLE_PRIVATE_KEY"
        in captured.err
    )


@pytest.mark.anyio
async def test_apple_callback_logs_client_secret_generation_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", "not-a-private-key")
    site = await _start_apple_provider_site(tmp_path)
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )
    captured = capsys.readouterr()

    assert response.status_code == 404
    assert response.json()["detail"] == "Apple login is not available."
    assert "Apple client secret generation failed." in captured.err


@pytest.mark.anyio
async def test_apple_callback_resolves_existing_provider_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="linked@example.com",
        is_verified=True,
    )
    await _create_apple_provider_link(
        site,
        user_id=user_id,
        provider_subject="apple-subject",
        account_email="linked@example.com",
    )
    setattr(
        site.app.state,
        APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeAppleTokenClient(),
    )
    setattr(
        site.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeAppleIDTokenValidator(
            claims=AppleIDTokenClaims(
                subject="apple-subject",
                email="linked@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_apple_callback_auto_links_verified_email_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(
        tmp_path,
        providers_config=_apple_provider_config(email_match_linking_enabled=True),
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
        APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeAppleTokenClient(
            response=AppleTokenResponse(
                access_token="access-token",
                id_token="id-token",
                token_type="bearer",
                expires_in=300,
            )
        ),
    )
    setattr(
        site.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeAppleIDTokenValidator(
            claims=AppleIDTokenClaims(
                subject="apple-match-subject",
                email="match@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = TestClient(site.app)
    start = client.get("/account/providers/apple/login", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies
    assert (
        await _apple_provider_linked_user_id(
            site,
            provider_subject="apple-match-subject",
        )
        == user_id
    )


@pytest.mark.anyio
async def test_apple_callback_links_provider_to_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    site.app.state.secret_envelope_service = SecretEnvelopeService.for_testing()
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="current@example.com",
        is_verified=True,
    )
    setattr(
        site.app.state,
        APPLE_OAUTH_TOKEN_CLIENT_STATE_ATTRIBUTE,
        FakeAppleTokenClient(),
    )
    setattr(
        site.app.state,
        APPLE_ID_TOKEN_VALIDATOR_STATE_ATTRIBUTE,
        FakeAppleIDTokenValidator(
            claims=AppleIDTokenClaims(
                subject="apple-link-subject",
                email="current@example.com",
                email_verified=True,
                nonce="nonce",
            )
        ),
    )
    client = _authenticated_client(site, email="current@example.com")
    start = client.get("/account/providers/apple/link", follow_redirects=False)
    cookie_state = _apple_oauth_cookie_state(site, start)

    response = client.post(
        "/account/providers/apple/callback",
        data={"code": "code", "state": cookie_state.state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert (
        await _apple_provider_linked_user_id(
            site,
            provider_subject="apple-link-subject",
        )
        == user_id
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_name", "provider_label"),
    PROVIDER_PAGE_CASES,
)
async def test_login_page_shows_provider_sign_in_when_provider_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_name: str,
    provider_label: str,
) -> None:
    _set_available_provider_secret(monkeypatch, provider_name)
    site = await _start_provider_site(tmp_path=tmp_path, provider_name=provider_name)

    response = TestClient(site.app).get("/account/login?return_to=/dashboard")

    assert response.status_code == 200
    assert f"Sign in with {provider_label}" in response.text
    assert (
        f"/account/providers/{provider_name}/login?return_to=%2Fdashboard"
        in response.text
    )


@pytest.mark.anyio
async def test_login_page_hides_google_sign_in_when_provider_disabled(
    tmp_path: Path,
) -> None:
    google_label = _provider_case(GOOGLE_PROVIDER_NAME).label
    site = await _start_google_provider_site(
        tmp_path,
        providers_config=_google_provider_config(enabled=False),
    )

    response = TestClient(site.app).get("/account/login")

    assert response.status_code == 200
    assert f"Sign in with {google_label}" not in response.text


@pytest.mark.anyio
async def test_login_and_security_pages_hide_google_when_oauth_config_incomplete(
    tmp_path: Path,
) -> None:
    google_label = _provider_case(GOOGLE_PROVIDER_NAME).label
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
    assert f"Sign in with {google_label}" not in login_response.text
    assert security_response.status_code == 200
    assert "Provider sign-in" not in security_response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_name", "provider_label"),
    PROVIDER_PAGE_CASES,
)
async def test_security_page_shows_provider_link_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_name: str,
    provider_label: str,
) -> None:
    _set_available_provider_secret(monkeypatch, provider_name)
    site = await _start_provider_site(tmp_path=tmp_path, provider_name=provider_name)
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
    assert f"Link {provider_label}" in response.text
    assert (
        f"/account/providers/{provider_name}/link?return_to=%2Faccount%2Fsecurity"
        in response.text
    )


@pytest.mark.anyio
async def test_security_page_shows_google_unlink_and_password_disable_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    google_label = _provider_case(GOOGLE_PROVIDER_NAME).label
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
    assert (
        f"{google_label} sign-in is linked as google-user@example.test" in response.text
    )
    assert f'value="{provider_id}"' in response.text
    assert f"Link another {google_label} account" in response.text
    assert f"Unlink {google_label}" in response.text
    assert "Disable username/password login" in response.text


@pytest.mark.anyio
async def test_security_page_shows_github_unlink_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    github_label = _provider_case(GITHUB_PROVIDER_NAME).label
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
    assert (
        f"{github_label} sign-in is linked as github-user@example.test" in response.text
    )
    assert f'value="{provider_id}"' in response.text
    assert f"Link another {github_label} account" in response.text
    assert f"Unlink {github_label}" in response.text
    assert "Disable username/password login" in response.text


@pytest.mark.anyio
async def test_security_page_shows_apple_unlink_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apple_label = _provider_case(APPLE_PROVIDER_NAME).label
    monkeypatch.setenv("APPLE_PRIVATE_KEY", _apple_private_key_pem())
    site = await _start_apple_provider_site(tmp_path)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    provider_id = await _create_apple_provider_link(
        site,
        user_id=user_id,
        account_email="apple-user@example.test",
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert (
        f"{apple_label} sign-in is linked as apple-user@example.test" in response.text
    )
    assert f'value="{provider_id}"' in response.text
    assert f"Link another {apple_label} account" in response.text
    assert f"Unlink {apple_label}" in response.text
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
@pytest.mark.parametrize(
    ("provider_name", "provider_label"),
    PROVIDER_PAGE_CASES,
)
async def test_security_page_rejects_unlinking_last_provider_sign_in_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_name: str,
    provider_label: str,
) -> None:
    _set_available_provider_secret(monkeypatch, provider_name)
    site = await _start_provider_site(tmp_path=tmp_path, provider_name=provider_name)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    provider_id = await _create_provider_link(
        provider_name,
        site,
        user_id=user_id,
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
        f"/account/security/providers/{provider_name}/unlink",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "provider_id": provider_id,
        },
    )

    assert response.status_code == 400
    assert (
        f"Add another sign-in method before unlinking {provider_label}."
        in response.text
    )
    assert await _provider_linked_user_id(provider_name, site) == user_id


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
async def test_login_page_shows_passkey_button_when_enabled(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(tmp_path, auth_config=PASSKEY_AUTH_CONFIG)

    response = _security_page_client(site).get("/account/login?return_to=/dashboard")

    assert response.status_code == 200
    assert "Sign in with passkey" in response.text
    assert "/account/login/passkey/options" in response.text
    assert "/account/login/passkey/complete" in response.text
    assert "scripts/passkeys.js" in response.text


@pytest.mark.anyio
async def test_security_page_omits_passkey_section_when_disabled(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(tmp_path)
    await _create_auth_schema(site)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Passkey sign-in" not in response.text


@pytest.mark.anyio
async def test_security_page_shows_passkey_controls_when_enabled(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Passkey sign-in" in response.text
    assert "Add passkey" in response.text
    assert "/account/security/passkeys/register/options" in response.text
    assert "/account/security/passkeys/register/complete" in response.text
    assert "scripts/passkeys.js" in response.text


@pytest.mark.anyio
async def test_passkey_registration_rejects_unverified_user(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=False,
    )
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    login_page = client.get("/account/login")

    response = client.post(
        "/account/security/passkeys/register/options",
        headers=_csrf_header(login_page.text),
        json={},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "Verify your email before adding a passkey."
    assert await _active_passkey_count(site, user_id) == 0


@pytest.mark.anyio
async def test_passkey_registration_stores_verified_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_id = credential_id_to_text(b"new-passkey")
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    def verify_registration(_options, **kwargs):
        assert kwargs["expected_challenge"]
        return SimpleNamespace(
            credential_id=credential_id,
            public_key=b"verified-public-key",
            sign_count=1,
            user_verified=True,
            credential_device_type="multi_device",
            credential_backed_up=True,
            aaguid="test-aaguid",
            attestation_format="none",
        )

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_registration",
        verify_registration,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")
    headers = _csrf_header(security_page.text)
    options_response = client.post(
        "/account/security/passkeys/register/options",
        headers=headers,
        json={},
    )

    response = client.post(
        "/account/security/passkeys/register/complete",
        headers=headers,
        json={
            "challenge_id": options_response.json()["challenge_id"],
            "credential": {
                "id": credential_id,
                "response": {"transports": ["internal"]},
            },
            "label": "  Laptop key  ",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "registered",
        "redirect_to": "/account/security",
    }
    async with site.require_capability(DatabaseCapability).transaction() as session:
        store = TortoiseWebAuthnCredentialStore(session)
        credential = await store.get_webauthn_credential(credential_id)
        assert credential is not None
        assert credential.label == "Laptop key"
        assert credential.public_key == b"verified-public-key"
        assert credential.transports == ("internal",)


@pytest.mark.anyio
async def test_passkey_registration_failure_consumes_challenge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    def reject_registration(_options, **_kwargs):
        raise passkey_pages.WebAuthnCeremonyError()

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_registration",
        reject_registration,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")
    headers = _csrf_header(security_page.text)
    options_response = client.post(
        "/account/security/passkeys/register/options",
        headers=headers,
        json={},
    )
    challenge_id = options_response.json()["challenge_id"]

    response = client.post(
        "/account/security/passkeys/register/complete",
        headers=headers,
        json={
            "challenge_id": challenge_id,
            "credential": {
                "id": credential_id_to_text(b"rejected-passkey"),
                "response": {"transports": ["internal"]},
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Passkey verification failed."
    assert await _active_passkey_count(site, user_id) == 0
    assert not await _authentication_challenge_exists(site, challenge_id)


@pytest.mark.anyio
async def test_passkey_registration_malformed_json_returns_controlled_error(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    client = _security_page_client(site)
    security_page = client.get("/account/security")
    headers = {
        **_csrf_header(security_page.text),
        "content-type": "application/json",
    }

    response = client.post(
        "/account/security/passkeys/register/complete",
        headers=headers,
        content="{",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Passkey verification failed."
    assert await _active_passkey_count(site, user_id) == 0


@pytest.mark.anyio
async def test_passkey_login_user_verified_assertion_satisfies_active_totp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config = {**PASSKEY_AUTH_CONFIG, "totp_mode": "opt_in"}
    credential_id = credential_id_to_text(b"login-passkey")
    site = await _start_security_site(tmp_path, auth_config=auth_config)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="passkey-login@example.com",
        is_verified=True,
    )
    await _create_passkey_credential(
        site,
        user_id=user_id,
        credential_id=credential_id,
    )
    await _create_active_totp_credential(site, user_id)

    def verify_authentication(_options, **kwargs):
        assert kwargs["stored_credential"].credential_id == credential_id
        return SimpleNamespace(
            credential_id=credential_id,
            sign_count=1,
            user_verified=True,
            credential_device_type="multi_device",
            credential_backed_up=True,
        )

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_authentication",
        verify_authentication,
    )
    client = _security_page_client(site)
    login_page = client.get("/account/login?return_to=/dashboard")
    headers = _csrf_header(login_page.text)
    options_response = client.post(
        "/account/login/passkey/options",
        headers=headers,
        json={
            "email": "passkey-login@example.com",
            "return_to": "/dashboard",
        },
    )

    response = client.post(
        "/account/login/passkey/complete",
        headers=headers,
        json={
            "challenge_id": options_response.json()["challenge_id"],
            "credential": {"id": credential_id},
            "return_to": "/dashboard",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redirect_to": "/dashboard"}
    assert TOTP_LOGIN_NONCE_COOKIE not in response.cookies
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name in response.cookies


@pytest.mark.anyio
async def test_passkey_login_user_verified_assertion_requires_totp_when_policy_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config = {
        **PASSKEY_AUTH_CONFIG,
        "totp_mode": "opt_in",
        "passkeys": {
            **PASSKEY_AUTH_CONFIG["passkeys"],
            "user_verification_satisfies_totp": False,
        },
    }
    credential_id = credential_id_to_text(b"login-passkey-totp-policy")
    site = await _start_security_site(tmp_path, auth_config=auth_config)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="passkey-policy@example.com",
        is_verified=True,
    )
    await _create_passkey_credential(
        site,
        user_id=user_id,
        credential_id=credential_id,
    )
    await _create_active_totp_credential(site, user_id)

    def verify_authentication(_options, **kwargs):
        assert kwargs["stored_credential"].credential_id == credential_id
        return SimpleNamespace(
            credential_id=credential_id,
            sign_count=1,
            user_verified=True,
            credential_device_type="multi_device",
            credential_backed_up=True,
        )

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_authentication",
        verify_authentication,
    )
    client = _security_page_client(site)
    login_page = client.get("/account/login?return_to=/dashboard")
    headers = _csrf_header(login_page.text)
    options_response = client.post(
        "/account/login/passkey/options",
        headers=headers,
        json={
            "email": "passkey-policy@example.com",
            "return_to": "/dashboard",
        },
    )

    response = client.post(
        "/account/login/passkey/complete",
        headers=headers,
        json={
            "challenge_id": options_response.json()["challenge_id"],
            "credential": {"id": credential_id},
            "return_to": "/dashboard",
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "totp_required"
    assert "challenge_step=totp" in payload["redirect_to"]
    assert TOTP_LOGIN_NONCE_COOKIE in response.cookies
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_passkey_login_possession_only_assertion_requires_totp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config = {**PASSKEY_AUTH_CONFIG, "totp_mode": "opt_in"}
    credential_id = credential_id_to_text(b"possession-passkey")
    site = await _start_security_site(tmp_path, auth_config=auth_config)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="passkey-totp@example.com",
        is_verified=True,
    )
    await _create_passkey_credential(
        site,
        user_id=user_id,
        credential_id=credential_id,
    )
    await _create_active_totp_credential(site, user_id)

    def verify_authentication(_options, **kwargs):
        assert kwargs["stored_credential"].credential_id == credential_id
        return SimpleNamespace(
            credential_id=credential_id,
            sign_count=1,
            user_verified=False,
            credential_device_type="multi_device",
            credential_backed_up=True,
        )

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_authentication",
        verify_authentication,
    )
    client = _security_page_client(site)
    login_page = client.get("/account/login?return_to=/dashboard")
    headers = _csrf_header(login_page.text)
    options_response = client.post(
        "/account/login/passkey/options",
        headers=headers,
        json={
            "email": "passkey-totp@example.com",
            "return_to": "/dashboard",
        },
    )

    response = client.post(
        "/account/login/passkey/complete",
        headers=headers,
        json={
            "challenge_id": options_response.json()["challenge_id"],
            "credential": {"id": credential_id},
            "return_to": "/dashboard",
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "totp_required"
    assert "challenge_step=totp" in payload["redirect_to"]
    assert TOTP_LOGIN_NONCE_COOKIE in response.cookies
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_passkey_login_keeps_verified_email_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_id = credential_id_to_text(b"unverified-passkey")
    site = await _start_security_site(tmp_path, auth_config=PASSKEY_AUTH_CONFIG)
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="unverified-passkey@example.com",
        is_verified=False,
    )
    await _create_passkey_credential(
        site,
        user_id=user_id,
        credential_id=credential_id,
    )

    def verify_authentication(_options, **kwargs):
        assert kwargs["stored_credential"].credential_id == credential_id
        return SimpleNamespace(
            credential_id=credential_id,
            sign_count=1,
            user_verified=True,
            credential_device_type="multi_device",
            credential_backed_up=True,
        )

    monkeypatch.setattr(
        passkey_pages,
        "verify_passkey_authentication",
        verify_authentication,
    )
    client = _security_page_client(site)
    login_page = client.get("/account/login")
    headers = _csrf_header(login_page.text)
    options_response = client.post(
        "/account/login/passkey/options",
        headers=headers,
        json={"email": "unverified-passkey@example.com"},
    )

    response = client.post(
        "/account/login/passkey/complete",
        headers=headers,
        json={
            "challenge_id": options_response.json()["challenge_id"],
            "credential": {"id": credential_id},
        },
    )

    assert response.status_code == 403
    assert response.json()["status"] == "email_verification_required"
    cookie_name = site.app.state.auth_settings.identity_options.session_cookie_name
    assert cookie_name not in response.cookies


@pytest.mark.anyio
async def test_security_page_rejects_removing_last_passkey_sign_in_method(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    await _set_password_login_enabled(site, user_id, False)
    row_id = await _create_passkey_credential(site, user_id=user_id)
    _override_current_user(
        site.app,
        user_id=user_id,
        hashed_password=None,
        password_login_enabled=False,
    )
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/passkeys/revoke",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "credential_id": row_id,
        },
    )

    assert response.status_code == 400
    assert "Add another sign-in method before removing this passkey." in response.text
    assert await _active_passkey_count(site, user_id) == 1


@pytest.mark.anyio
async def test_security_page_removes_passkey_when_password_login_remains(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    row_id = await _create_passkey_credential(site, user_id=user_id)
    client = _security_page_client(site)
    security_page = client.get("/account/security")

    response = client.post(
        "/account/security/passkeys/revoke",
        data={
            "csrf_token": _csrf_token(security_page.text),
            "credential_id": row_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/security"
    assert await _active_passkey_count(site, user_id) == 0


@pytest.mark.anyio
async def test_security_page_disables_password_login_when_passkey_registered(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(
        tmp_path,
        auth_config=PASSKEY_AUTH_CONFIG,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    await _create_passkey_credential(site, user_id=user_id)
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
async def test_totp_disable_allows_passkey_as_remaining_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config = {**PASSKEY_AUTH_CONFIG, "totp_mode": "opt_in"}
    site = await _start_security_site(
        tmp_path,
        auth_config=auth_config,
    )
    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)
    await _set_password_login_enabled(site, user_id, False)
    secret, _recovery_codes = await _create_active_totp_credential(site, user_id)
    await _create_passkey_credential(site, user_id=user_id)
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

    await _create_auth_schema(site)
    user_id = await _create_local_user(
        site,
        email="security@example.com",
        is_verified=True,
    )
    _override_current_user(site.app, user_id=user_id)

    response = _security_page_client(site).get("/account/security")

    assert response.status_code == 200
    assert "Login &amp; Security" in response.text
    assert "security@example.test" in response.text


@pytest.mark.anyio
async def test_security_page_omits_totp_section_when_totp_disabled(
    tmp_path: Path,
) -> None:
    site = await _start_security_site(tmp_path)
    await _create_auth_schema(site)

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
        recovery_store = TortoiseRecoveryCodeStore(
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
        recovery_store = TortoiseRecoveryCodeStore(
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
