from __future__ import annotations

import base64
import importlib.util
import secrets as secret_tokens
import sys
import zlib
from collections.abc import Mapping
from functools import cache
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from wybra.auth.capabilities import setup_site as setup_auth_site
from wybra.config import ConfigService, MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.secrets import (
    AwsSecretsManagerSourceDriver,
    CryptoSecretSourceSettings,
    DefaultSecretsCapability,
    EnvironmentSecretSourceDriver,
    InvalidSecretKeyError,
    KeychainSecretSourceDriver,
    KeychainSecretSourceSettings,
    KmsSecretSourceSettings,
    MissingSecretError,
    MissingSecretSourceDependencyError,
    SecretsCapability,
    SecretsSettings,
    SecretValue,
    UnknownSecretSourceError,
    UnsupportedSecretSourceError,
    VaultSecretSourceDriver,
    VaultSecretSourceSettings,
    module_config,
)
from wybra.secrets.capabilities import setup_site as setup_secrets_site
from wybra.secrets.source_errors import (
    aws_error_code,
    aws_secret_missing,
    keyring_reports_missing_secret,
    vault_secret_missing,
    vault_secret_value,
)
from wybra.services.crypto import ENVELOPE_PREFIX, SecretEnvelopeService
from wybra.site import Site, start

_KEYRING_PLATFORM_SKIP_REASONS = {
    "macos": (
        "macOS Keychain integration requires Darwin, keyring, and an "
        "accessible Keychain."
    ),
    "linux": "Linux Secret Service integration requires D-Bus and a provider.",
    "windows": (
        "Windows Credential Manager integration requires Windows, keyring, and "
        "an accessible Credential Manager backend."
    ),
}


@cache
def _keyring_backend_available(probe_platform: str) -> bool:
    if not _keyring_platform_matches(probe_platform):
        return False
    if importlib.util.find_spec("keyring") is None:
        return False
    try:
        import keyring

        keyring.get_password(_keyring_probe_service(), _keyring_probe_username())
    except Exception:
        return False
    return True


def _keyring_platform_matches(probe_platform: str) -> bool:
    if probe_platform == "linux":
        return sys.platform.startswith("linux")
    if probe_platform == "macos":
        return sys.platform == "darwin"
    if probe_platform == "windows":
        return sys.platform == "win32"
    raise ValueError(f"Unsupported keyring probe platform: {probe_platform}")


def _keyring_probe_service() -> str:
    return secret_tokens.token_hex(8)


def _keyring_probe_username() -> str:
    return secret_tokens.token_hex(8)


def _crypto_key_entry(version: str = "current") -> str:
    key = Fernet.generate_key()
    checksum = f"{(zlib.crc32(base64.urlsafe_b64decode(key)) & 0xFFFFFFFF):08x}"
    return f"{version}:{key.decode('ascii')}:{checksum}"


class TestSecretValue:
    def test_redacts_from_string_like_diagnostics(self) -> None:
        secret = SecretValue("actual-secret", source="environment", key="TOKEN")

        assert secret.reveal() == "actual-secret"
        assert str(secret) == "<redacted-secret>"
        assert repr(secret) == "SecretValue(<redacted>)"
        assert f"{secret}" == "<redacted-secret>"
        assert "actual-secret" not in repr(secret)


class TestSecretsSettingsCredentialReferences:
    def test_credential_references_expose_crypto_key_metadata_only(self) -> None:
        settings = SecretsSettings(
            crypto=CryptoSecretSourceSettings(
                source="keychain",
                current_key="secrets/key/current",
                previous_keys="secrets/key/previous",
            )
        )

        references = settings.credential_references()

        assert [
            (
                reference.name,
                reference.key,
                reference.owner,
                reference.source,
                reference.required,
                reference.rotation_role,
            )
            for reference in references
        ] == [
            (
                "secret",
                "secrets/key/current",
                "crypto",
                "keychain",
                True,
                "current",
            ),
            (
                "secret-prev",
                "secrets/key/previous",
                "crypto",
                "keychain",
                False,
                "previous",
            ),
        ]
        assert all(not hasattr(reference, "value") for reference in references)

    def test_credential_references_are_empty_without_crypto_source(self) -> None:
        assert SecretsSettings().credential_references() == ()


class TestDefaultSecretsCapability:
    def test_resolves_and_checks_registered_source(self) -> None:
        capability = DefaultSecretsCapability.from_drivers(
            (EnvironmentSecretSourceDriver({"API_TOKEN": "resolved"}),)
        )

        assert capability.exists("environment", "API_TOKEN") is True
        assert capability.resolve("environment", "API_TOKEN").reveal() == "resolved"

    def test_rejects_source_outside_literal_set(self) -> None:
        capability = DefaultSecretsCapability.from_drivers(())

        with pytest.raises(UnsupportedSecretSourceError, match="must be one of"):
            capability.exists("unsupported", "TOKEN")

    def test_rejects_supported_but_unregistered_source(self) -> None:
        capability = DefaultSecretsCapability.from_drivers(())

        with pytest.raises(UnknownSecretSourceError, match="source=environment"):
            capability.resolve("environment", "TOKEN")


class TestEnvironmentSource:
    def test_resolves_environment_key_without_exposing_value(self) -> None:
        driver = EnvironmentSecretSourceDriver({"SERVICE_SECRET": "secret-value"})

        value = driver.resolve("SERVICE_SECRET")

        assert value.reveal() == "secret-value"
        assert "secret-value" not in repr(value)

    def test_reports_missing_key(self) -> None:
        driver = EnvironmentSecretSourceDriver({})

        assert driver.exists("SERVICE_SECRET") is False
        with pytest.raises(MissingSecretError, match="SERVICE_SECRET"):
            driver.resolve("SERVICE_SECRET")

    @pytest.mark.parametrize("key", ["", "invalid-name", "1TOKEN", "TOKEN VALUE"])
    def test_rejects_invalid_environment_keys(self, key: str) -> None:
        driver = EnvironmentSecretSourceDriver({})

        with pytest.raises(InvalidSecretKeyError, match="Environment secret key"):
            driver.exists(key)


class FakeAwsClient:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.described: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        if SecretId not in self.values:
            raise FakeAwsClientError("ResourceNotFoundException")
        return {"SecretString": self.values[SecretId]}

    def describe_secret(self, *, SecretId: str) -> dict[str, object]:
        self.described.append(SecretId)
        if SecretId not in self.values:
            raise FakeAwsClientError("ResourceNotFoundException")
        return {}


class FakeAwsClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class TestAwsSecretsManagerSource:
    def test_error_helpers_classify_missing_secret(self) -> None:
        exc = FakeAwsClientError("ResourceNotFoundException")

        assert aws_error_code(exc) == "ResourceNotFoundException"
        assert aws_secret_missing(exc) is True

    def test_resolves_with_base_path(self) -> None:
        client = FakeAwsClient({"production/wybra/client-secret": "secret"})
        driver = AwsSecretsManagerSourceDriver(
            KmsSecretSourceSettings(base_path="/production/wybra"),
            client=client,
        )

        assert driver.exists("client-secret") is True
        assert driver.resolve("client-secret").reveal() == "secret"

    def test_missing_secret_maps_to_domain_error(self) -> None:
        driver = AwsSecretsManagerSourceDriver(
            KmsSecretSourceSettings(),
            client=FakeAwsClient(),
        )

        assert driver.exists("missing") is False
        with pytest.raises(MissingSecretError, match="missing"):
            driver.resolve("missing")

    def test_missing_optional_dependency_is_actionable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def missing_import(name: str) -> Any:
            raise ModuleNotFoundError(name=name)

        monkeypatch.setattr(
            "wybra.secrets.sources.importlib.import_module", missing_import
        )
        driver = AwsSecretsManagerSourceDriver(KmsSecretSourceSettings())

        with pytest.raises(MissingSecretSourceDependencyError, match="`kms`"):
            driver.exists("client-secret")


class FakeVaultKvV2:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def read_secret_version(self, *, path: str, mount_point: str) -> dict[str, object]:
        if path not in self.values:
            raise FakeVaultMissing()
        return {"data": {"data": {"value": self.values[path]}}}

    def read_secret_metadata(self, *, path: str, mount_point: str) -> dict[str, object]:
        if path not in self.values:
            raise FakeVaultMissing()
        return {}


class FakeVaultClient:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.secrets = FakeVaultSecrets(values)


class FakeVaultSecrets:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.kv = FakeVaultKv(values)


class FakeVaultKv:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.v2 = FakeVaultKvV2(values)


class FakeVaultResponse:
    status_code = 404


class FakeVaultMissing(Exception):
    response = FakeVaultResponse()


class TestVaultSource:
    def test_error_helpers_classify_missing_secret_and_parse_values(self) -> None:
        assert vault_secret_missing(FakeVaultMissing()) is True
        assert vault_secret_value({"data": {"data": {"value": "secret"}}}) == "secret"

    def test_resolves_with_secrets_path(self) -> None:
        driver = VaultSecretSourceDriver(
            VaultSecretSourceSettings(secrets_path="apps/wybra"),
            client=FakeVaultClient({"apps/wybra/google": "vault-secret"}),
        )

        assert driver.exists("google") is True
        assert driver.resolve("google").reveal() == "vault-secret"

    def test_missing_secret_maps_to_domain_error(self) -> None:
        driver = VaultSecretSourceDriver(
            VaultSecretSourceSettings(),
            client=FakeVaultClient(),
        )

        assert driver.exists("missing") is False
        with pytest.raises(MissingSecretError, match="missing"):
            driver.resolve("missing")

    def test_missing_optional_dependency_is_actionable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def missing_import(name: str) -> Any:
            raise ModuleNotFoundError(name=name)

        monkeypatch.setattr(
            "wybra.secrets.sources.importlib.import_module", missing_import
        )
        driver = VaultSecretSourceDriver(VaultSecretSourceSettings())

        with pytest.raises(MissingSecretSourceDependencyError, match="`vault`"):
            driver.exists("client-secret")


class FakeKeyring:
    def __init__(self, values: Mapping[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})
        self.requests: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.requests.append((service, username))
        return self.values.get((service, username))


class FakeMacosMissingKeyring:
    class KeyringError(Exception):
        pass

    def get_password(self, service: str, username: str) -> str | None:
        raise self.KeyringError(
            "Can't get password from keychain: (-50, 'Unknown Error')"
        )


class TestKeychainSource:
    def test_error_helper_classifies_macos_missing_item(self) -> None:
        exc = FakeMacosMissingKeyring.KeyringError(
            "Can't get password from keychain: (-50, 'Unknown Error')"
        )

        assert keyring_reports_missing_secret(exc) is True

    def test_driver_uses_keyring_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        keyring = FakeKeyring({("wybra", "api-token"): "keyring-secret"})
        driver = KeychainSecretSourceDriver(
            KeychainSecretSourceSettings(appname="wybra", username="deployment"),
            keyring_module=keyring,
        )

        assert driver.exists("api-token") is True
        assert driver.resolve("api-token").reveal() == "keyring-secret"
        assert keyring.requests == [
            ("wybra", "api-token"),
            ("wybra", "api-token"),
        ]

    def test_windows_driver_uses_keyring_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        driver = KeychainSecretSourceDriver(
            KeychainSecretSourceSettings(appname="wybra"),
            keyring_module=FakeKeyring({("wybra", "api-token"): "windows-secret"}),
        )

        assert driver.exists("api-token") is True
        assert driver.resolve("api-token").reveal() == "windows-secret"

    def test_macos_missing_item_status_is_reported_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        driver = KeychainSecretSourceDriver(
            KeychainSecretSourceSettings(appname="wybra"),
            keyring_module=FakeMacosMissingKeyring(),
        )

        assert driver.exists("missing-key") is False
        with pytest.raises(MissingSecretError, match="missing-key"):
            driver.resolve("missing-key")

    def test_linux_missing_dependency_is_actionable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        def missing_import(name: str) -> Any:
            raise ModuleNotFoundError(name=name)

        monkeypatch.setattr(
            "wybra.secrets.sources.importlib.import_module", missing_import
        )
        driver = KeychainSecretSourceDriver(KeychainSecretSourceSettings())

        with pytest.raises(MissingSecretSourceDependencyError, match="`keychain`"):
            driver.exists("api-token")

    @pytest.mark.parametrize(
        "_probe_platform",
        [
            pytest.param("macos", id="macos"),
            pytest.param("linux", id="linux"),
            pytest.param("windows", id="windows"),
        ],
    )
    def test_platform_keychain_missing_key_is_reported(
        self,
        _probe_platform: str,
    ) -> None:
        if not _keyring_backend_available(_probe_platform):
            pytest.skip(_KEYRING_PLATFORM_SKIP_REASONS[_probe_platform])

        driver = KeychainSecretSourceDriver(
            KeychainSecretSourceSettings(appname=_keyring_probe_service())
        )

        assert driver.exists(_keyring_probe_username()) is False


class TestSecretsSettings:
    def test_loads_source_specific_non_secret_metadata(self) -> None:
        settings = SecretsSettings.load_settings(
            ConfigService(
                [
                    MappingConfigSource(
                        {
                            "secrets.crypto": {
                                "source": "keychain",
                                "current_key": "secrets/key/dev/current",
                                "previous_keys": "secrets/key/dev/previous",
                            },
                            "secrets.kms": {
                                "region_name": "ap-southeast-2",
                                "base_path": "/production/wybra",
                            },
                            "secrets.vault": {
                                "mount_point": "kv",
                                "secrets_path": "apps/wybra",
                            },
                            "secrets.keychain": {
                                "appname": "uniquode.io",
                                "username": "deployment",
                            },
                        }
                    )
                ],
                config_defs=(module_config,),
                discover_module_config=False,
            )
        )

        assert settings.crypto == CryptoSecretSourceSettings(
            source="keychain",
            current_key="secrets/key/dev/current",
            previous_keys="secrets/key/dev/previous",
        )
        assert settings.kms.region_name == "ap-southeast-2"
        assert settings.kms.base_path == "/production/wybra"
        assert settings.vault.mount_point == "kv"
        assert settings.vault.secrets_path == "apps/wybra"
        assert settings.keychain.appname == "uniquode.io"
        assert settings.keychain.username == "deployment"


@pytest.mark.anyio
async def test_secrets_setup_site_registers_environment_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("wybra.secrets",)}}),
        environ={"SERVICE_TOKEN": "from-environment"},
    )

    capability = site.require_capability(SecretsCapability)

    assert capability.resolve("environment", "SERVICE_TOKEN").reveal() == (
        "from-environment"
    )


@pytest.mark.anyio
async def test_auth_setup_uses_secrets_backed_secret_envelope_service() -> None:
    key_entry = _crypto_key_entry()
    ConfigService.set_runtime_environment({"SYSTEM_SECRET_KEY": key_entry})
    site = Site(
        FastAPI(),
        ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {
                            "modules": ("wybra.secrets", "wybra.auth"),
                            "database_url": "sqlite:///app.sqlite3",
                        },
                        "secrets.crypto": {
                            "source": "environment",
                            "current_key": "SYSTEM_SECRET_KEY",
                        },
                    }
                )
            ],
        ),
    )
    await setup_secrets_site(site)

    await setup_auth_site(site)

    service = site.app.state.secret_envelope_service
    assert isinstance(service, SecretEnvelopeService)
    assert service.encrypt_required("secret").startswith(f"{ENVELOPE_PREFIX}|current|")


@pytest.mark.anyio
async def test_auth_setup_requires_secrets_capability_for_crypto_source() -> None:
    ConfigService.set_runtime_environment({"SYSTEM_SECRET_KEY": _crypto_key_entry()})
    site = Site(
        FastAPI(),
        ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {
                            "modules": ("wybra.auth",),
                            "database_url": "sqlite:///app.sqlite3",
                        },
                        "secrets.crypto": {
                            "source": "environment",
                            "current_key": "SYSTEM_SECRET_KEY",
                        },
                    }
                )
            ],
        ),
    )

    with pytest.raises(ConfigurationError, match="no SecretsCapability"):
        await setup_auth_site(site)
