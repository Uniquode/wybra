"""Versioned secret-value encryption helpers for Wevra services."""

from __future__ import annotations

import base64
import binascii
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from wevra.auth.configuration import ConfigurationError

ENVELOPE_PREFIX: Final = "WEVRA:SECRET"
SECRET_KEY_LENGTH: Final = 32
ENVELOPE_SEPARATOR: Final = "|"
ENV_IDENTITY_PROVIDER_SECRET_KEY_CURRENT: Final = "IDENTITY_PROVIDER_SECRET_KEY_CURRENT"
ENV_IDENTITY_PROVIDER_SECRET_KEY_LEGACY: Final = "IDENTITY_PROVIDER_SECRET_KEY_LEGACY"


class SecretDataError(ConfigurationError):
    """Raised when secret envelopes or key material are malformed."""


class SecretMaterialMissingError(ConfigurationError):
    """Raised when required keys are not available."""


class SecretVersionError(SecretDataError):
    """Raised when an envelope references an unknown key version."""


def _checksum(value: bytes) -> str:
    return f"{zlib.crc32(value) & 0xFFFFFFFF:08x}"


def _normalise_environment_value(value: str | None) -> str | None:
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None


def _decode_base64_key(raw_key: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(raw_key)
    except (binascii.Error, ValueError) as exc:
        raise SecretDataError(
            "Secret key value must be valid URL-safe base64."
        ) from exc

    if len(key) != SECRET_KEY_LENGTH:
        raise SecretDataError(
            f"Secret key value must decode to {SECRET_KEY_LENGTH} bytes, "
            f"not {len(key)}."
        )

    return key


def parse_secret_key_entry(raw_entry: str) -> tuple[str, bytes]:
    """Parse a single ``version:key:checksum`` entry."""

    if not isinstance(raw_entry, str):
        raise SecretDataError("Secret key entry must be a string.")

    entry = raw_entry.strip()
    parts = entry.split(":", 2)
    if len(parts) != 3:
        raise SecretDataError(
            "Secret key entries must use format <version>:<base64-key>:<crc32hex>."
        )

    version, encoded_key, encoded_checksum = parts
    if not version:
        raise SecretDataError("Secret key version must not be empty.")

    key = _decode_base64_key(encoded_key)
    actual_checksum = _checksum(key)
    if len(encoded_checksum) != 8:
        raise SecretDataError(
            f"Secret key checksum for version {version} must be 8 hexadecimal "
            "characters."
        )

    try:
        int(encoded_checksum, 16)
    except ValueError as exc:
        raise SecretDataError(
            f"Secret key checksum for version {version} is not valid hex."
        ) from exc

    if encoded_checksum.lower() != actual_checksum:
        raise SecretDataError(
            f"Secret key checksum mismatch for version {version}: "
            f"{encoded_checksum.lower()} expected {actual_checksum}."
        )

    return version, key


def parse_secret_key_bundle(
    current: str | None,
    legacy: str | None = None,
) -> SecretKeyRing:
    """Parse key material into a runtime key ring."""

    current_key = _normalise_environment_value(current)
    if current_key is None:
        raise SecretMaterialMissingError("Current secret key is not configured.")

    ring_entries: list[tuple[str, bytes]] = [parse_secret_key_entry(current_key)]

    legacy_value = _normalise_environment_value(legacy)
    if legacy_value is not None:
        for raw_entry in legacy_value.split(","):
            if not raw_entry.strip():
                raise SecretDataError(
                    "Legacy secret key entries must be comma-separated and non-empty."
                )
            ring_entries.append(parse_secret_key_entry(raw_entry))

    versions = [version for version, _ in ring_entries]
    if len(set(versions)) != len(versions):
        raise SecretDataError("Secret key versions must be unique.")

    return SecretKeyRing(
        current_version=ring_entries[0][0],
        keys=tuple(
            SecretKey(version=version, key=key) for version, key in ring_entries
        ),
    )


def parse_secret_key_ring_from_env(
    env: Mapping[str, str] | None,
) -> SecretKeyRing | None:
    """Create a key ring from environment-style settings.

    Returns ``None`` when neither current nor legacy key is configured.
    """

    if env is None:
        return None

    current = _normalise_environment_value(
        env.get(ENV_IDENTITY_PROVIDER_SECRET_KEY_CURRENT)
    )
    legacy = _normalise_environment_value(
        env.get(ENV_IDENTITY_PROVIDER_SECRET_KEY_LEGACY)
    )

    if current is None and legacy is None:
        return None

    return parse_secret_key_bundle(current=current, legacy=legacy)


@dataclass(frozen=True, slots=True)
class SecretKey:
    version: str
    key: bytes

    @property
    def fernet(self) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(self.key))


@dataclass(frozen=True, slots=True)
class SecretKeyRing:
    current_version: str
    keys: tuple[SecretKey, ...]

    @property
    def current(self) -> SecretKey:
        for key in self.keys:
            if key.version == self.current_version:
                return key

        raise SecretVersionError(
            f"Current secret version is missing from key ring: {self.current_version}."
        )

    def fernet_for(self, version: str) -> Fernet:
        for key in self.keys:
            if key.version == version:
                return key.fernet

        raise SecretVersionError(f"Unknown secret version: {version}.")


def _encode_envelope(version: str, token: str) -> str:
    return ENVELOPE_SEPARATOR.join((ENVELOPE_PREFIX, version, token))


def _decode_envelope(value: str) -> tuple[str, str] | None:
    if not value.startswith(ENVELOPE_PREFIX + ENVELOPE_SEPARATOR):
        return None

    prefix, version, encrypted = value.split(ENVELOPE_SEPARATOR, 2)
    if prefix != ENVELOPE_PREFIX:
        return None

    return version, encrypted


class SecretEnvelopeService:
    """Encrypt and decrypt secret values with versioned key material."""

    def __init__(self, key_loader: Callable[[], SecretKeyRing | None]):
        self._key_loader = key_loader
        self._key_ring: SecretKeyRing | None = None
        self._key_ring_loaded = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None) -> SecretEnvelopeService:
        return cls(lambda: parse_secret_key_ring_from_env(env))

    @classmethod
    def from_key_bundle(
        cls,
        current: str | None,
        legacy: str | None = None,
    ) -> SecretEnvelopeService:
        return cls(lambda: parse_secret_key_bundle(current=current, legacy=legacy))

    def _get_key_ring(self, *, required: bool) -> SecretKeyRing | None:
        if not self._key_ring_loaded:
            self._key_ring = self._key_loader()
            self._key_ring_loaded = True

        if self._key_ring is None and required:
            raise SecretMaterialMissingError(
                "Secret material is required but no keys are configured."
            )

        return self._key_ring

    def encrypt(self, value: str, *, required: bool = False) -> str:
        key_ring = self._get_key_ring(required=required)
        if key_ring is None:
            return value

        encrypted = key_ring.current.fernet.encrypt(value.encode("utf-8"))
        return _encode_envelope(key_ring.current.version, encrypted.decode("ascii"))

    def decrypt(self, value: str, *, required: bool = False) -> tuple[str, str]:
        envelope = _decode_envelope(value)
        if envelope is None:
            return value, "plaintext"

        key_ring = self._get_key_ring(required=required)
        if key_ring is None:
            return value, envelope[0]

        version, token = envelope
        fernet = key_ring.fernet_for(version)
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8"), version
        except InvalidToken as exc:
            raise SecretDataError(
                "Encrypted secret value is invalid or corrupt."
            ) from exc


__all__ = [
    "ENVELOPE_PREFIX",
    "ENV_IDENTITY_PROVIDER_SECRET_KEY_CURRENT",
    "ENV_IDENTITY_PROVIDER_SECRET_KEY_LEGACY",
    "SecretDataError",
    "SecretMaterialMissingError",
    "SecretVersionError",
    "SecretEnvelopeService",
    "SecretKey",
    "SecretKeyRing",
    "parse_secret_key_bundle",
    "parse_secret_key_ring_from_env",
    "parse_secret_key_entry",
]
