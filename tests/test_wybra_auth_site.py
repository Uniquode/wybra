import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wybra.auth import AuthCapability, anonymous_required, login_required
from wybra.auth.delivery import NullIdentityDelivery
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    SqlAlchemyRecoveryCodeStore,
    SqlAlchemyTOTPCredentialStore,
)
from wybra.auth.mfa.totp import generate_totp, generate_totp_secret
from wybra.auth.models import metadata as auth_metadata
from wybra.auth.routes.pages import totp_management as totp_management_pages
from wybra.config import MappingConfigSource
from wybra.db import DatabaseCapability
from wybra.services.crypto import SecretEnvelopeService
from wybra.site import SiteCapabilityError, start

PAGE_MODULES = (
    "wybra.forms",
    "wybra.assets",
    "wybra.template",
    "wybra.db",
    "wybra.auth",
)


def _site_config_source(
    tmp_path: Path,
    *,
    modules: tuple[str, ...] = ("wybra.forms", "wybra.db", "wybra.auth"),
    auth_config: dict[str, object] | None = None,
    account_prefix: str = "/account",
) -> MappingConfigSource:
    config: dict[str, object] = {
        "app": {
            "config_path": tmp_path / "app.toml",
            "project_root": tmp_path,
            "modules": modules,
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
        },
        "app.routes": {
            "prefixes": {
                "wybra.auth": {"account": account_prefix, "api": ""},
            }
        },
        "app.templates": {"auto_reload": True, "cache_size": 0},
        "app.assets": {"url_path": "/static/", "root": Path("static")},
    }
    if auth_config is not None:
        config["auth"] = auth_config
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


def _override_current_user(app: FastAPI, user_id: uuid.UUID | None = None) -> None:
    async def current_user() -> SimpleNamespace:
        return SimpleNamespace(
            id=user_id or uuid.uuid4(),
            email="security@example.test",
            is_active=True,
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
