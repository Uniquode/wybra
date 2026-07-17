from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from wybra.core.environment import environment_get, environment_is_set
from wybra.secrets.config import (
    KeychainSecretSourceSettings,
    KmsSecretSourceSettings,
    VaultSecretSourceSettings,
)
from wybra.secrets.source_errors import (
    aws_secret_missing,
    keyring_reports_missing_secret,
    raise_aws_secret_source_error,
    raise_keyring_secret_source_error,
    raise_vault_secret_source_error,
    vault_secret_missing,
    vault_secret_value,
)
from wybra.services.secrets import (
    ENVIRONMENT_SOURCE,
    KEYCHAIN_SOURCE,
    KMS_SOURCE,
    VAULT_SOURCE,
    InvalidSecretKeyError,
    MissingSecretError,
    MissingSecretSourceDependencyError,
    SecretSource,
    SecretSourceUnavailableError,
    SecretValue,
    secret_key_value,
)

ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@runtime_checkable
class SecretSourceDriver(Protocol):
    @property
    def source(self) -> SecretSource: ...

    def resolve(self, key: str) -> SecretValue: ...

    def exists(self, key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class EnvironmentSecretSourceDriver:
    environ: object
    source: SecretSource = ENVIRONMENT_SOURCE

    def resolve(self, key: str) -> SecretValue:
        env_key = environment_key_name(key)
        value = environment_get(self.environ, env_key)
        if value is None:
            raise MissingSecretError(source=self.source, key=env_key)
        return SecretValue(value, source=self.source, key=env_key)

    def exists(self, key: str) -> bool:
        return environment_is_set(self.environ, environment_key_name(key))


@dataclass(frozen=True, slots=True)
class AwsSecretsManagerSourceDriver:
    settings: KmsSecretSourceSettings
    client: Any | None = None
    source: SecretSource = KMS_SOURCE

    def resolve(self, key: str) -> SecretValue:
        secret_id = _qualified_key(self.settings.base_path, secret_key_value(key))
        client = self._client()
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:
            raise_aws_secret_source_error(
                exc,
                source=self.source,
                key=secret_id,
                operation="get",
            )
        value = response.get("SecretString")
        if value is None and response.get("SecretBinary") is not None:
            value = _binary_secret_value(response["SecretBinary"])
        if value is None:
            raise MissingSecretError(source=self.source, key=secret_id)
        return SecretValue(value, source=self.source, key=secret_id)

    def exists(self, key: str) -> bool:
        secret_id = _qualified_key(self.settings.base_path, secret_key_value(key))
        client = self._client()
        try:
            client.describe_secret(SecretId=secret_id)
        except Exception as exc:
            if aws_secret_missing(exc):
                return False
            raise_aws_secret_source_error(
                exc,
                source=self.source,
                key=secret_id,
                operation="exists",
            )
        return True

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        boto3 = _import_optional_dependency(
            "boto3",
            source=self.source,
            extra="kms",
        )
        session_kwargs: dict[str, str] = {}
        if self.settings.region_name is not None:
            session_kwargs["region_name"] = self.settings.region_name
        return boto3.session.Session(**session_kwargs).client("secretsmanager")


@dataclass(frozen=True, slots=True)
class VaultSecretSourceDriver:
    settings: VaultSecretSourceSettings
    client: Any | None = None
    source: SecretSource = VAULT_SOURCE

    def resolve(self, key: str) -> SecretValue:
        path = _qualified_key(self.settings.secrets_path, secret_key_value(key))
        client = self._client()
        try:
            response = client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point=self.settings.mount_point,
            )
        except Exception as exc:
            raise_vault_secret_source_error(
                exc,
                source=self.source,
                key=path,
                operation="get",
            )
        value = vault_secret_value(response)
        if value is None:
            raise MissingSecretError(source=self.source, key=path)
        return SecretValue(value, source=self.source, key=path)

    def exists(self, key: str) -> bool:
        path = _qualified_key(self.settings.secrets_path, secret_key_value(key))
        client = self._client()
        try:
            client.secrets.kv.v2.read_secret_metadata(
                path=path,
                mount_point=self.settings.mount_point,
            )
        except Exception as exc:
            if vault_secret_missing(exc):
                return False
            raise_vault_secret_source_error(
                exc,
                source=self.source,
                key=path,
                operation="exists",
            )
        return True

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        hvac = _import_optional_dependency(
            "hvac",
            source=self.source,
            extra="vault",
        )
        client = (
            hvac.Client(url=self.settings.url) if self.settings.url else hvac.Client()
        )
        authenticated = getattr(client, "is_authenticated", None)
        if callable(authenticated) and not authenticated():
            raise SecretSourceUnavailableError(
                source=self.source,
                reason="Vault client is not authenticated.",
            )
        return client


@dataclass(frozen=True, slots=True)
class KeychainSecretSourceDriver:
    settings: KeychainSecretSourceSettings
    keyring_module: Any | None = None
    source: SecretSource = KEYCHAIN_SOURCE

    def resolve(self, key: str) -> SecretValue:
        key_value = secret_key_value(key)
        if _keyring_platform_available():
            return self._resolve_keyring(key_value)
        raise SecretSourceUnavailableError(
            source=self.source,
            key=key_value,
            reason=f"Unsupported keychain platform: {sys.platform}.",
        )

    def exists(self, key: str) -> bool:
        key_value = secret_key_value(key)
        if _keyring_platform_available():
            return self._exists_keyring(key_value)
        raise SecretSourceUnavailableError(
            source=self.source,
            key=key_value,
            reason=f"Unsupported keychain platform: {sys.platform}.",
        )

    def store(self, key: str, value: str) -> None:
        key_value = secret_key_value(key)
        if not isinstance(value, str) or value == "":
            raise InvalidSecretKeyError(
                source=self.source,
                key=key_value,
                message="Keychain secret value must be a non-empty string.",
            )
        if _keyring_platform_available():
            self._store_keyring(key_value, value)
            return
        raise SecretSourceUnavailableError(
            source=self.source,
            key=key_value,
            reason=f"Unsupported keychain platform: {sys.platform}.",
        )

    def identity(self, key: str) -> tuple[str, str]:
        """Return the keychain service and account used for this secret."""
        key_value = secret_key_value(key)
        return self._keyring_service(), key_value

    def _resolve_keyring(self, key: str) -> SecretValue:
        keyring = self._keyring()
        try:
            value = keyring.get_password(
                self._keyring_service(),
                key,
            )
        except Exception as exc:
            if keyring_reports_missing_secret(exc):
                raise MissingSecretError(source=self.source, key=key) from exc
            raise_keyring_secret_source_error(
                exc,
                source=self.source,
                key=key,
                operation="read",
            )
        if value is None:
            raise MissingSecretError(source=self.source, key=key)
        return SecretValue(str(value), source=self.source, key=key)

    def _exists_keyring(self, key: str) -> bool:
        keyring = self._keyring()
        try:
            return (
                keyring.get_password(
                    self._keyring_service(),
                    key,
                )
                is not None
            )
        except Exception as exc:
            if keyring_reports_missing_secret(exc):
                return False
            raise_keyring_secret_source_error(
                exc,
                source=self.source,
                key=key,
                operation="exists",
            )
        return False

    def _store_keyring(self, key: str, value: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(
                self._keyring_service(),
                key,
                value,
            )
        except Exception as exc:
            raise_keyring_secret_source_error(
                exc,
                source=self.source,
                key=key,
                operation="write",
            )

    def _keyring(self) -> Any:
        if self.keyring_module is not None:
            return self.keyring_module
        return _import_keyring()

    def _keyring_service(self) -> str:
        return self.settings.appname


def environment_key_name(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise InvalidSecretKeyError(
            source=ENVIRONMENT_SOURCE,
            key=key,
            message=(
                "Environment secret key must match "
                f"{ENVIRONMENT_KEY_PATTERN.pattern!r}."
            ),
        )
    value = key.strip()
    if ENVIRONMENT_KEY_PATTERN.fullmatch(value):
        return value
    raise InvalidSecretKeyError(
        source=ENVIRONMENT_SOURCE,
        key=key,
        message=(
            f"Environment secret key must match {ENVIRONMENT_KEY_PATTERN.pattern!r}."
        ),
    )


def _keyring_platform_available() -> bool:
    return (
        sys.platform == "darwin"
        or sys.platform == "win32"
        or sys.platform.startswith("linux")
    )


def _import_optional_dependency(module_name: str, *, source: str, extra: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise MissingSecretSourceDependencyError(
            source=source,
            dependency=module_name,
            hint=f"Install Wybra with the `{extra}` optional dependency extra.",
        ) from exc


def _import_keyring() -> Any:
    try:
        return importlib.import_module("keyring")
    except ModuleNotFoundError as exc:
        if exc.name != "keyring":
            raise
        raise MissingSecretSourceDependencyError(
            source=KEYCHAIN_SOURCE,
            dependency="keyring",
            hint=(
                "Install Wybra with the `keychain` optional dependency extra "
                "and ensure the operating system keychain backend is available."
            ),
        ) from exc


def _qualified_key(base_path: str | None, key: str) -> str:
    if base_path is None:
        return key
    prefix = base_path.strip("/")
    suffix = key.strip("/")
    if not prefix:
        return suffix
    if not suffix:
        return prefix
    return f"{prefix}/{suffix}"


def _binary_secret_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


__all__ = (
    "AwsSecretsManagerSourceDriver",
    "EnvironmentSecretSourceDriver",
    "KeychainSecretSourceDriver",
    "SecretSourceDriver",
    "VaultSecretSourceDriver",
    "environment_key_name",
)
