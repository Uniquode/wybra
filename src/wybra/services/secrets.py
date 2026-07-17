"""Shared secret reference contracts for Wybra services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, TypeGuard, runtime_checkable

SecretSource = Literal["environment", "kms", "keychain", "vault"]

ENVIRONMENT_SOURCE: Final[SecretSource] = "environment"
KMS_SOURCE: Final[SecretSource] = "kms"
KEYCHAIN_SOURCE: Final[SecretSource] = "keychain"
VAULT_SOURCE: Final[SecretSource] = "vault"
SECRET_SOURCES: Final[frozenset[SecretSource]] = frozenset(
    (ENVIRONMENT_SOURCE, KMS_SOURCE, KEYCHAIN_SOURCE, VAULT_SOURCE)
)


class SecretsError(RuntimeError):
    """Base class for secrets-domain failures."""


class SecretsConfigurationError(SecretsError):
    """Raised when configured secrets behaviour cannot be supported."""


class UnsupportedSecretSourceError(SecretsConfigurationError):
    """Raised when a source value is outside the supported literal set."""

    def __init__(self, *, source: object, message: str | None = None) -> None:
        source_text = _safe_reference(source)
        detail = message or f"Unsupported secret source: source={source_text}."
        super().__init__(detail)
        self.source = source


class UnknownSecretSourceError(SecretsError):
    """Raised when a supported source has no registered driver."""

    def __init__(self, *, source: str) -> None:
        super().__init__(f"Unknown secret source: source={source}.")
        self.source = source


class MissingSecretError(SecretsError):
    """Raised when a key does not exist in the selected source."""

    def __init__(self, *, source: str, key: object) -> None:
        key_text = _safe_reference(key)
        super().__init__(f"Missing secret: source={source}, key={key_text}.")
        self.source = source
        self.key = key


class InvalidSecretKeyError(SecretsError):
    """Raised when a key reference is malformed for a source."""

    def __init__(
        self,
        *,
        key: object,
        source: str | None = None,
        message: str | None = None,
    ) -> None:
        key_text = _safe_reference(key)
        source_text = f"source={source}, " if source is not None else ""
        detail = message or "Secret key reference is invalid."
        super().__init__(f"{detail} {source_text}key={key_text}.")
        self.source = source
        self.key = key


class MissingSecretSourceDependencyError(SecretsConfigurationError):
    """Raised when a source-specific optional dependency is unavailable."""

    def __init__(
        self,
        *,
        source: str,
        dependency: str,
        hint: str,
    ) -> None:
        super().__init__(
            "Secret source dependency is missing: "
            f"source={source}, dependency={dependency}. {hint}"
        )
        self.source = source
        self.dependency = dependency
        self.hint = hint


class SecretSourceUnavailableError(SecretsError):
    """Raised when a source exists but cannot be reached or used."""

    def __init__(
        self,
        *,
        source: str,
        key: object | None = None,
        reason: str,
    ) -> None:
        key_text = f", key={_safe_reference(key)}" if key is not None else ""
        super().__init__(
            f"Secret source is unavailable: source={source}{key_text}, reason={reason}."
        )
        self.source = source
        self.key = key
        self.reason = reason


class SecretSourceOperationError(SecretsError):
    """Raised when a source operation fails after the source is available."""

    def __init__(
        self,
        *,
        source: str,
        key: object | None = None,
        operation: str,
        reason: str,
    ) -> None:
        key_text = f", key={_safe_reference(key)}" if key is not None else ""
        super().__init__(
            "Secret source operation failed: "
            f"source={source}, operation={operation}{key_text}, reason={reason}."
        )
        self.source = source
        self.key = key
        self.operation = operation
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SecretValue:
    """Resolved secret value that redacts itself in diagnostic contexts."""

    _value: str = field(repr=False)
    source: str | None = field(default=None, repr=False)
    key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._value, str):
            object.__setattr__(self, "_value", str(self._value))

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "<redacted-secret>"

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)


@runtime_checkable
class SecretsCapability(Protocol):
    def resolve(self, source: SecretSource | str, key: str) -> SecretValue: ...

    def exists(self, source: SecretSource | str, key: str) -> bool: ...


def normalise_secret_source(
    value: object,
    *,
    name: str = "secret source",
) -> SecretSource:
    if isinstance(value, str) and _is_secret_source(value):
        return value
    allowed = ", ".join(sorted(SECRET_SOURCES))
    raise UnsupportedSecretSourceError(
        source=value,
        message=f"{name} must be one of: {allowed}.",
    )


def _is_secret_source(value: str) -> TypeGuard[SecretSource]:
    return value in SECRET_SOURCES


def secret_key_value(value: object, *, name: str = "secret key") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise InvalidSecretKeyError(
        key=value, message=f"{name} must be a non-blank string."
    )


def _safe_reference(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return repr(value)


__all__ = (
    "ENVIRONMENT_SOURCE",
    "InvalidSecretKeyError",
    "KEYCHAIN_SOURCE",
    "KMS_SOURCE",
    "MissingSecretError",
    "MissingSecretSourceDependencyError",
    "SECRET_SOURCES",
    "SecretSource",
    "SecretSourceOperationError",
    "SecretSourceUnavailableError",
    "SecretValue",
    "SecretsCapability",
    "SecretsConfigurationError",
    "SecretsError",
    "UnknownSecretSourceError",
    "UnsupportedSecretSourceError",
    "VAULT_SOURCE",
    "normalise_secret_source",
    "secret_key_value",
)
