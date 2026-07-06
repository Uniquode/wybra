import asyncio
import importlib
import json
from collections.abc import Mapping
from pathlib import Path

from click.testing import CliRunner
from sqlalchemy import select

import wybra.secrets.cli as secret_cli
import wybra.secrets.sources as secret_sources
from support_database import sqlite_file_url
from wybra.auth.mfa.storage import TOTP_ACTIVE_STATUS
from wybra.auth.models import (
    IdentityProvider,
    IdentityTotpCredential,
    IdentityTotpRecoveryCode,
    User,
)
from wybra.db.models import Base
from wybra.db.persistence import close_database, create_database, session_scope
from wybra.forms import CSRF_TOKEN_SECRET_KEY_CURRENT, CSRF_TOKEN_SECRET_KEY_PREVIOUS
from wybra.services.crypto import (
    SecretEnvelopeService,
    generate_secret_key_entry,
    parse_secret_key_bundle,
)


class FakeKeyring:
    def __init__(self, values: Mapping[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.writes.append((service, username, value))
        self.values[(service, username)] = value


def _install_fake_keyring(monkeypatch, keyring: FakeKeyring) -> None:
    def import_module(name: str):
        if name == "keyring":
            return keyring
        return importlib.import_module(name)

    monkeypatch.setattr(secret_sources.sys, "platform", "linux")
    monkeypatch.setattr(secret_sources.importlib, "import_module", import_module)


def _app_config(path: Path) -> Path:
    path.write_text(
        """
[app]
modules = ["wybra.secrets", "wybra.forms", "wybra.auth", "wybra.providers"]

[wybra.forms]
csrf_token_secret_source = "keychain"

[secrets.keychain]
appname = "uniquode.io"
username = "deployment"

[secrets.crypto]
source = "keychain"
current_key = "SYSTEM_SECRET_KEY"
previous_keys = "SYSTEM_SECRET_KEYS_PREVIOUS"

[auth.providers.google]
enabled = true
secrets = "keychain"
client_secret_key = "auth/providers/google/client-secret"

[auth.providers.github]
enabled = false
secrets = "keychain"
client_secret_key = "auth/providers/github/client-secret"

[auth.providers.apple]
enabled = true
client_id = "com.example.app.web"
team_id = "TEAMID1234"
key_id = "KEYID1234"
secrets = "environment"
private_key_secret_key = "APPLE_PRIVATE_KEY"
""".strip(),
        encoding="utf-8",
    )
    return path


def _non_keychain_crypto_config(path: Path) -> Path:
    path.write_text(
        """
[app]
modules = ["wybra.secrets"]

[secrets.crypto]
source = "environment"
current_key = "SYSTEM_SECRET_KEY"
previous_keys = "SYSTEM_SECRET_KEYS_PREVIOUS"
""".strip(),
        encoding="utf-8",
    )
    return path


def _keychain_crypto_without_previous_config(path: Path) -> Path:
    path.write_text(
        """
[app]
modules = ["wybra.secrets"]

[secrets.keychain]
appname = "uniquode.io"

[secrets.crypto]
source = "keychain"
current_key = "SYSTEM_SECRET_KEY"
""".strip(),
        encoding="utf-8",
    )
    return path


def _non_keychain_csrf_config(path: Path) -> Path:
    path.write_text(
        """
[app]
modules = ["wybra.secrets", "wybra.forms"]

[wybra.forms]
csrf_token_secret = "inline-csrf-secret"
""".strip(),
        encoding="utf-8",
    )
    return path


def _reencrypt_app_config(path: Path, database_url: str) -> Path:
    path.write_text(
        f"""
[app]
modules = ["wybra.secrets", "wybra.auth"]
database_url = "{database_url}"

[app.templates]
auto_reload = true
cache_size = 0

[app.assets]
url_path = "/static/"
root = "static"

[secrets.keychain]
appname = "uniquode.io"
username = "deployment"

[secrets.crypto]
source = "keychain"
current_key = "SYSTEM_SECRET_KEY"
previous_keys = "SYSTEM_SECRET_KEYS_PREVIOUS"
""".strip(),
        encoding="utf-8",
    )
    return path


async def _create_reencrypt_database(
    database_url: str,
    *,
    provider_access_token: str,
    provider_refresh_token: str | None,
    totp_secret: str,
    recovery_code_verifier: str,
) -> None:
    database = create_database(database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope(database.session_factory) as session:
            user = User(
                email="user@example.com",
                hashed_password="hashed-password",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
            session.add(user)
            await session.flush()
            session.add(
                IdentityProvider(
                    provider_name="google",
                    provider_subject="provider-subject",
                    crypt_access_token=provider_access_token,
                    crypt_refresh_token=provider_refresh_token,
                    account_email="user@example.com",
                )
            )
            credential = IdentityTotpCredential(
                user_id=user.id,
                crypt_secret=totp_secret,
                status=TOTP_ACTIVE_STATUS,
                created_at=1000.0,
                activated_at=1001.0,
            )
            session.add(credential)
            await session.flush()
            session.add(
                IdentityTotpRecoveryCode(
                    credential_id=credential.id,
                    code_verifier=recovery_code_verifier,
                    created_at=1002.0,
                )
            )
            await session.commit()
    finally:
        await close_database(database)


async def _reencrypt_database_values(database_url: str) -> dict[str, str | None]:
    database = create_database(database_url)
    try:
        async with session_scope(database.session_factory) as session:
            provider = (await session.execute(select(IdentityProvider))).scalar_one()
            credential = (
                await session.execute(select(IdentityTotpCredential))
            ).scalar_one()
            recovery_code = (
                await session.execute(select(IdentityTotpRecoveryCode))
            ).scalar_one()
            return {
                "access": provider.crypt_access_token,
                "refresh": provider.crypt_refresh_token,
                "totp": credential.crypt_secret,
                "recovery": recovery_code.code_verifier,
            }
    finally:
        await close_database(database)


def test_set_get_and_list_use_default_keychain_mapping(monkeypatch) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)
    runner = CliRunner()

    result = runner.invoke(
        secret_cli.secret_command,
        ["set", "WYBRA_SECRET_KEY_CURRENT"],
        input="secret-value\n",
    )

    assert result.exit_code == 0, result.output
    assert keyring.values[("wybra", "WYBRA_SECRET_KEY_CURRENT")] == "secret-value"

    result = runner.invoke(
        secret_cli.secret_command,
        ["get", "--json", "WYBRA_SECRET_KEY_CURRENT"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "key": "WYBRA_SECRET_KEY_CURRENT",
        "service": "wybra",
        "username": "WYBRA_SECRET_KEY_CURRENT",
        "value": "secret-value",
    }

    result = runner.invoke(secret_cli.secret_command, ["list", "--json"])

    assert result.exit_code == 0, result.output
    records = {item["key"]: item for item in json.loads(result.output)["keys"]}
    assert records["WYBRA_SECRET_KEY_CURRENT"]["exists"] is True
    assert records["WYBRA_SECRET_KEYS_PREVIOUS"]["exists"] is False


def test_set_supports_json_bulk_input(monkeypatch) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["set", "--json"],
        input=json.dumps(
            {
                "WYBRA_SECRET_KEY_CURRENT": "current-secret",
                "auth/providers/google/client-secret": "google-secret",
            }
        ),
    )

    assert result.exit_code == 0, result.output
    assert keyring.values == {
        ("wybra", "WYBRA_SECRET_KEY_CURRENT"): "current-secret",
        ("wybra", "auth/providers/google/client-secret"): "google-secret",
    }
    output = json.loads(result.output)
    assert output == {
        "stored": [
            {
                "key": "WYBRA_SECRET_KEY_CURRENT",
                "service": "wybra",
                "username": "WYBRA_SECRET_KEY_CURRENT",
            },
            {
                "key": "auth/providers/google/client-secret",
                "service": "wybra",
                "username": "auth/providers/google/client-secret",
            },
        ]
    }
    assert "google-secret" not in result.output


def test_list_uses_configured_keychain_metadata_and_app_key_refs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring(
        {
            ("uniquode.io", "SYSTEM_SECRET_KEY"): "current",
            (
                "uniquode.io",
                "auth/providers/google/client-secret",
            ): "google",
            ("uniquode.io", CSRF_TOKEN_SECRET_KEY_CURRENT): "csrf",
            ("uniquode.io", CSRF_TOKEN_SECRET_KEY_PREVIOUS): "previous-csrf",
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _app_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "list", "--json"],
    )

    assert result.exit_code == 0, result.output
    records = {item["key"]: item for item in json.loads(result.output)["keys"]}
    assert records["SYSTEM_SECRET_KEY"]["service"] == "uniquode.io"
    assert records["SYSTEM_SECRET_KEY"]["username"] == "SYSTEM_SECRET_KEY"
    assert records["SYSTEM_SECRET_KEY"]["exists"] is True
    assert records["SYSTEM_SECRET_KEYS_PREVIOUS"]["exists"] is False
    assert records[CSRF_TOKEN_SECRET_KEY_CURRENT]["owner"] == "forms"
    assert records[CSRF_TOKEN_SECRET_KEY_CURRENT]["description"] == (
        "Forms CSRF token secret."
    )
    assert records[CSRF_TOKEN_SECRET_KEY_CURRENT]["exists"] is True
    assert records[CSRF_TOKEN_SECRET_KEY_PREVIOUS]["owner"] == "forms"
    assert records[CSRF_TOKEN_SECRET_KEY_PREVIOUS]["description"] == (
        "Forms CSRF token secret."
    )
    assert records[CSRF_TOKEN_SECRET_KEY_PREVIOUS]["exists"] is True
    assert records["auth/providers/google/client-secret"]["exists"] is True
    assert "WYBRA_SECRET_KEY_CURRENT" not in records
    assert "WYBRA_SECRET_KEYS_PREVIOUS" not in records
    assert "auth/providers/github/client-secret" not in records
    assert "APPLE_PRIVATE_KEY" not in records


def test_list_excludes_crypto_keys_for_non_keychain_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
[app]
modules = ["wybra.secrets"]

[secrets.crypto]
source = "environment"
current_key = "SYSTEM_SECRET_KEY"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "list", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"keys": []}


def test_list_excludes_csrf_fallback_without_keychain_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
[app]
modules = ["wybra.secrets", "wybra.forms"]

[wybra.forms]
csrf_token_secret = "inline-csrf-secret"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "list", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {"keys": []}


def test_get_honours_app_config_environment(monkeypatch, tmp_path: Path) -> None:
    keyring = FakeKeyring({("uniquode.io", "SYSTEM_SECRET_KEY"): "configured-secret"})
    _install_fake_keyring(monkeypatch, keyring)
    monkeypatch.setenv("APP_CONFIG", _app_config(tmp_path / "app.toml").as_posix())

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["get", "SYSTEM_SECRET_KEY"],
    )

    assert result.exit_code == 0
    assert result.output == "configured-secret\n"


def test_rotate_secret_key_updates_previous_before_current(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current = generate_secret_key_entry(version="current")
    previous = generate_secret_key_entry(version="previous")
    keyring = FakeKeyring(
        {
            ("uniquode.io", "SYSTEM_SECRET_KEY"): current,
            ("uniquode.io", "SYSTEM_SECRET_KEYS_PREVIOUS"): previous,
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _app_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "rotate", "secret-key", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["target"] == "secret-key"
    assert payload["old_current_version"] == "current"
    assert payload["new_current_version"] not in {"current", "previous"}
    assert payload["previous_key_count"] == 2
    new_current = keyring.values[("uniquode.io", "SYSTEM_SECRET_KEY")]
    new_previous = keyring.values[("uniquode.io", "SYSTEM_SECRET_KEYS_PREVIOUS")]
    assert new_previous == f"{current},{previous}"
    parse_secret_key_bundle(current=new_current, previous=new_previous)
    assert [write[1] for write in keyring.writes[:2]] == [
        "SYSTEM_SECRET_KEYS_PREVIOUS",
        "SYSTEM_SECRET_KEY",
    ]
    assert current not in result.output
    assert previous not in result.output
    assert new_current not in result.output


def test_rotate_secret_key_dry_run_does_not_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current = generate_secret_key_entry(version="current")
    keyring = FakeKeyring({("uniquode.io", "SYSTEM_SECRET_KEY"): current})
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _app_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        [
            "--config",
            config_path.as_posix(),
            "rotate",
            "secret-key",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["previous_key_count"] == 1
    assert keyring.values == {("uniquode.io", "SYSTEM_SECRET_KEY"): current}
    assert keyring.writes == []
    assert current not in result.output


def test_rotate_secret_key_refuses_non_keychain_crypto_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring({("uniquode.io", "SYSTEM_SECRET_KEY"): "unchanged"})
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _non_keychain_crypto_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "rotate", "secret-key"],
    )

    assert result.exit_code != 0
    assert "keychain-backed system secret keys" in result.output
    assert keyring.values == {("uniquode.io", "SYSTEM_SECRET_KEY"): "unchanged"}
    assert keyring.writes == []


def test_rotate_secret_key_refuses_missing_previous_keys_reference(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current = generate_secret_key_entry(version="current")
    keyring = FakeKeyring({("uniquode.io", "SYSTEM_SECRET_KEY"): current})
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _keychain_crypto_without_previous_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "rotate", "secret-key"],
    )

    assert result.exit_code != 0
    assert "[secrets.crypto].previous_keys" in result.output
    assert keyring.values == {("uniquode.io", "SYSTEM_SECRET_KEY"): current}
    assert keyring.writes == []


def test_rotate_csrf_token_secret_updates_previous_before_current(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring(
        {
            ("uniquode.io", CSRF_TOKEN_SECRET_KEY_CURRENT): "current-csrf-secret",
            (
                "uniquode.io",
                CSRF_TOKEN_SECRET_KEY_PREVIOUS,
            ): "previous-csrf-secret",
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _app_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        [
            "--config",
            config_path.as_posix(),
            "rotate",
            "csrf-token-secret",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["target"] == "csrf-token-secret"
    assert payload["previous_secret_count"] == 2
    new_current = keyring.values[("uniquode.io", CSRF_TOKEN_SECRET_KEY_CURRENT)]
    new_previous = keyring.values[("uniquode.io", CSRF_TOKEN_SECRET_KEY_PREVIOUS)]
    assert new_current != "current-csrf-secret"
    assert new_previous == "current-csrf-secret,previous-csrf-secret"
    assert [write[1] for write in keyring.writes[:2]] == [
        CSRF_TOKEN_SECRET_KEY_PREVIOUS,
        CSRF_TOKEN_SECRET_KEY_CURRENT,
    ]
    assert "current-csrf-secret" not in result.output
    assert "previous-csrf-secret" not in result.output
    assert new_current not in result.output


def test_rotate_csrf_token_secret_refuses_non_keychain_forms_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _non_keychain_csrf_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "rotate", "csrf-token-secret"],
    )

    assert result.exit_code != 0
    assert "keychain-backed CSRF token secrets" in result.output
    assert keyring.values == {}
    assert keyring.writes == []


def test_reencrypt_secrets_dry_run_reports_without_writing_database_rows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_key = generate_secret_key_entry(version="current")
    previous_key = generate_secret_key_entry(version="previous")
    current_service = SecretEnvelopeService.from_key_bundle(current_key, previous_key)
    previous_service = SecretEnvelopeService.from_key_bundle(previous_key)
    old_access = previous_service.encrypt_required("access-token")
    old_refresh = previous_service.encrypt_required("refresh-token")
    old_totp = previous_service.encrypt_required("totp-secret")
    recovery_verifier = previous_service.create_verifier_required(
        "recovery-code",
        context="totp-recovery-code",
    )
    database_url = sqlite_file_url(tmp_path / "reencrypt.sqlite3")
    asyncio.run(
        _create_reencrypt_database(
            database_url,
            provider_access_token=old_access,
            provider_refresh_token=old_refresh,
            totp_secret=old_totp,
            recovery_code_verifier=recovery_verifier,
        )
    )
    keyring = FakeKeyring(
        {
            ("uniquode.io", "SYSTEM_SECRET_KEY"): current_key,
            ("uniquode.io", "SYSTEM_SECRET_KEYS_PREVIOUS"): previous_key,
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _reencrypt_app_config(tmp_path / "app.toml", database_url)

    result = CliRunner().invoke(
        secret_cli.secret_command,
        [
            "--config",
            config_path.as_posix(),
            "reencrypt-secrets",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["target"] == "reencrypt-secrets"
    assert payload["dry_run"] is True
    assert payload["scanned"] == 3
    assert payload["rewritten"] == 3
    assert payload["unsupported_recovery_code_verifiers"] == 1
    values = asyncio.run(_reencrypt_database_values(database_url))
    assert values == {
        "access": old_access,
        "refresh": old_refresh,
        "totp": old_totp,
        "recovery": recovery_verifier,
    }
    assert current_key not in result.output
    assert previous_key not in result.output
    assert old_access not in result.output
    assert old_refresh not in result.output
    assert old_totp not in result.output
    assert "access-token" not in result.output
    assert "refresh-token" not in result.output
    assert "totp-secret" not in result.output
    assert "recovery-code" not in result.output
    assert current_service.current_version_required() == "current"


def test_reencrypt_secrets_rewrites_previous_version_provider_and_totp_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_key = generate_secret_key_entry(version="current")
    previous_key = generate_secret_key_entry(version="previous")
    current_service = SecretEnvelopeService.from_key_bundle(current_key, previous_key)
    previous_service = SecretEnvelopeService.from_key_bundle(previous_key)
    old_access = previous_service.encrypt_required("access-token")
    old_refresh = previous_service.encrypt_required("refresh-token")
    old_totp = previous_service.encrypt_required("totp-secret")
    recovery_verifier = previous_service.create_verifier_required(
        "recovery-code",
        context="totp-recovery-code",
    )
    database_url = sqlite_file_url(tmp_path / "reencrypt.sqlite3")
    asyncio.run(
        _create_reencrypt_database(
            database_url,
            provider_access_token=old_access,
            provider_refresh_token=old_refresh,
            totp_secret=old_totp,
            recovery_code_verifier=recovery_verifier,
        )
    )
    keyring = FakeKeyring(
        {
            ("uniquode.io", "SYSTEM_SECRET_KEY"): current_key,
            ("uniquode.io", "SYSTEM_SECRET_KEYS_PREVIOUS"): previous_key,
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _reencrypt_app_config(tmp_path / "app.toml", database_url)

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "reencrypt-secrets", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["rewritten"] == 3
    assert payload["unsupported_recovery_code_verifiers"] == 1
    values = asyncio.run(_reencrypt_database_values(database_url))
    assert values["access"] != old_access
    assert values["refresh"] != old_refresh
    assert values["totp"] != old_totp
    assert values["recovery"] == recovery_verifier
    assert current_service.decrypt_required(values["access"] or "") == (
        "access-token",
        "current",
    )
    assert current_service.decrypt_required(values["refresh"] or "") == (
        "refresh-token",
        "current",
    )
    assert current_service.decrypt_required(values["totp"] or "") == (
        "totp-secret",
        "current",
    )
    assert current_key not in result.output
    assert previous_key not in result.output
    assert old_access not in result.output
    assert old_refresh not in result.output
    assert old_totp not in result.output
    assert "access-token" not in result.output
    assert "refresh-token" not in result.output
    assert "totp-secret" not in result.output


def test_reencrypt_secrets_skips_current_and_plaintext_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_key = generate_secret_key_entry(version="current")
    previous_key = generate_secret_key_entry(version="previous")
    current_service = SecretEnvelopeService.from_key_bundle(current_key, previous_key)
    current_access = current_service.encrypt_required("access-token")
    current_totp = current_service.encrypt_required("totp-secret")
    recovery_verifier = current_service.create_verifier_required(
        "recovery-code",
        context="totp-recovery-code",
    )
    database_url = sqlite_file_url(tmp_path / "reencrypt.sqlite3")
    asyncio.run(
        _create_reencrypt_database(
            database_url,
            provider_access_token=current_access,
            provider_refresh_token="plaintext-refresh-token",
            totp_secret=current_totp,
            recovery_code_verifier=recovery_verifier,
        )
    )
    keyring = FakeKeyring(
        {
            ("uniquode.io", "SYSTEM_SECRET_KEY"): current_key,
            ("uniquode.io", "SYSTEM_SECRET_KEYS_PREVIOUS"): previous_key,
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _reencrypt_app_config(tmp_path / "app.toml", database_url)

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "reencrypt-secrets", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned"] == 3
    assert payload["rewritten"] == 0
    assert payload["skipped_current"] == 2
    assert payload["skipped_plaintext"] == 1
    values = asyncio.run(_reencrypt_database_values(database_url))
    assert values == {
        "access": current_access,
        "refresh": "plaintext-refresh-token",
        "totp": current_totp,
        "recovery": recovery_verifier,
    }
    assert current_key not in result.output
    assert previous_key not in result.output
    assert current_access not in result.output
    assert current_totp not in result.output
    assert "access-token" not in result.output
    assert "plaintext-refresh-token" not in result.output
    assert "totp-secret" not in result.output


def test_blank_config_option_is_rejected() -> None:
    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", "   ", "list"],
    )

    assert result.exit_code == 2
    assert "--config must not be blank" in result.output
