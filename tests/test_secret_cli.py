import importlib
import json
from collections.abc import Mapping
from pathlib import Path

from click.testing import CliRunner

import wybra.secrets.cli as secret_cli
import wybra.secrets.sources as secret_sources


class FakeKeyring:
    def __init__(self, values: Mapping[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
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
modules = ["wybra.secrets", "wybra.auth"]

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
secrets = "environment"
client_secret_key = "APPLE_SECRET"
""".strip(),
        encoding="utf-8",
    )
    return path


def test_set_get_and_list_use_default_keychain_mapping(monkeypatch) -> None:
    keyring = FakeKeyring()
    _install_fake_keyring(monkeypatch, keyring)
    runner = CliRunner()

    result = runner.invoke(
        secret_cli.secret_command,
        ["set", "WYBRA_SECRET_KEY_CURRENT"],
        input="secret-value\n",
    )

    assert result.exit_code == 0
    assert keyring.values[("wybra", "WYBRA_SECRET_KEY_CURRENT")] == "secret-value"

    result = runner.invoke(
        secret_cli.secret_command,
        ["get", "--json", "WYBRA_SECRET_KEY_CURRENT"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "key": "WYBRA_SECRET_KEY_CURRENT",
        "service": "wybra",
        "username": "WYBRA_SECRET_KEY_CURRENT",
        "value": "secret-value",
    }

    result = runner.invoke(secret_cli.secret_command, ["list", "--json"])

    assert result.exit_code == 0
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

    assert result.exit_code == 0
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
            ("uniquode.io", "deployment:SYSTEM_SECRET_KEY"): "current",
            (
                "uniquode.io",
                "deployment:auth/providers/google/client-secret",
            ): "google",
        }
    )
    _install_fake_keyring(monkeypatch, keyring)
    config_path = _app_config(tmp_path / "app.toml")

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", config_path.as_posix(), "list", "--json"],
    )

    assert result.exit_code == 0
    records = {item["key"]: item for item in json.loads(result.output)["keys"]}
    assert records["SYSTEM_SECRET_KEY"]["service"] == "uniquode.io"
    assert records["SYSTEM_SECRET_KEY"]["username"] == "deployment:SYSTEM_SECRET_KEY"
    assert records["SYSTEM_SECRET_KEY"]["exists"] is True
    assert records["SYSTEM_SECRET_KEYS_PREVIOUS"]["exists"] is False
    assert records["auth/providers/google/client-secret"]["exists"] is True
    assert "WYBRA_SECRET_KEY_CURRENT" not in records
    assert "WYBRA_SECRET_KEYS_PREVIOUS" not in records
    assert "auth/providers/github/client-secret" not in records
    assert "APPLE_SECRET" not in records


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

    assert result.exit_code == 0
    assert json.loads(result.output) == {"keys": []}


def test_get_honours_app_config_environment(monkeypatch, tmp_path: Path) -> None:
    keyring = FakeKeyring(
        {("uniquode.io", "deployment:SYSTEM_SECRET_KEY"): "configured-secret"}
    )
    _install_fake_keyring(monkeypatch, keyring)
    monkeypatch.setenv("APP_CONFIG", _app_config(tmp_path / "app.toml").as_posix())

    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["get", "SYSTEM_SECRET_KEY"],
    )

    assert result.exit_code == 0
    assert result.output == "configured-secret\n"


def test_blank_config_option_is_rejected() -> None:
    result = CliRunner().invoke(
        secret_cli.secret_command,
        ["--config", "   ", "list"],
    )

    assert result.exit_code == 2
    assert "--config must not be blank" in result.output
