"""Versioned secret-value encryption helpers for Wybra services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Final, Protocol

from cryptography.fernet import Fernet, InvalidToken

from wybra.core.environment import environment_get
from wybra.core.exceptions import ConfigurationError
from wybra.services.secrets import (
    MissingSecretError,
    SecretsError,
    SecretSource,
    SecretValue,
    normalise_secret_source,
)

ENVELOPE_PREFIX: Final = "WYBRA:SECRET"
VERIFIER_PREFIX: Final = "WYBRA:VERIFIER"
PLAIN_TEXT_VERSION: Final = "__wybra_plaintext__"
SECRET_KEY_LENGTH: Final = 32
ENVELOPE_SEPARATOR: Final = "|"
ENV_WYBRA_SECRET_KEY_CURRENT: Final = "WYBRA_SECRET_KEY_CURRENT"
ENV_WYBRA_SECRET_KEYS_PREVIOUS: Final = "WYBRA_SECRET_KEYS_PREVIOUS"


class SecretDataError(ConfigurationError):
    """Raised when secret envelopes or key material are malformed."""


class SecretMaterialMissingError(ConfigurationError):
    """Raised when required keys are not available."""


class SecretVersionError(SecretDataError):
    """Raised when an envelope references an unknown key version."""


class SecretKeySecretSource(Protocol):
    """Secret source for text key-entry material.

    Crypto keys resolved through this protocol use the same
    ``version:base64-key:checksum`` text format as environment-backed keys.
    """

    def resolve(self, source: SecretSource, key: str) -> SecretValue: ...


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
    if version == PLAIN_TEXT_VERSION:
        raise SecretDataError(
            "Secret key version must not use reserved plaintext marker."
        )
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
            f"expected {actual_checksum}, got {encoded_checksum.lower()}."
        )

    return version, key


def parse_secret_key_bundle(
    current: str | None,
    previous: str | None = None,
) -> SecretKeyRing:
    """Parse key material into a runtime key ring."""

    current_key = _normalise_environment_value(current)
    if current_key is None:
        raise SecretMaterialMissingError("Current secret key is not configured.")

    ring_entries: list[tuple[str, bytes]] = [parse_secret_key_entry(current_key)]

    previous_value = _normalise_environment_value(previous)
    if previous_value is not None:
        for raw_entry in previous_value.split(","):
            if not raw_entry.strip():
                raise SecretDataError(
                    "Previous secret key entries must be comma-separated and non-empty."
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


@dataclass(frozen=True, slots=True)
class SecretKeyRotationPlan:
    """Validated key material for a keychain-backed system secret-key rotation."""

    current_value: str = field(repr=False)
    previous_value: str = field(repr=False)
    retired_version: str
    new_version: str
    previous_key_count: int


def generate_secret_key_entry(
    *,
    version: str | None = None,
    existing_versions: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Generate a parser-compatible system secret-key entry for rotation."""

    used_versions = set(existing_versions)
    if version is not None and version in used_versions:
        raise SecretDataError("Generated secret key version must be unique.")

    for _attempt in range(32):
        candidate_version = version or f"rot-{int(time.time())}-{token_hex(4)}"
        if candidate_version in used_versions:
            if version is not None:
                raise SecretDataError("Generated secret key version must be unique.")
            continue

        encoded_key = Fernet.generate_key().decode("ascii")
        key = _decode_base64_key(encoded_key)
        entry = f"{candidate_version}:{encoded_key}:{_checksum(key)}"
        parse_secret_key_entry(entry)
        return entry

    raise SecretDataError("Could not generate a unique secret key version.")


def plan_secret_key_rotation(
    *,
    current: str | None,
    previous: str | None,
) -> SecretKeyRotationPlan:
    """Build a validated rotation plan without writing keychain state."""

    current_value = _normalise_environment_value(current)
    previous_value = _normalise_environment_value(previous)
    key_ring = parse_secret_key_bundle(current=current_value, previous=previous_value)
    assert current_value is not None
    existing_versions = {key.version for key in key_ring.keys}
    new_current = generate_secret_key_entry(existing_versions=existing_versions)
    new_version, _new_key = parse_secret_key_entry(new_current)
    previous_entries = [current_value]
    if previous_value is not None:
        previous_entries.extend(previous_value.split(","))
    new_previous = ",".join(previous_entries)
    parse_secret_key_bundle(current=new_current, previous=new_previous)
    return SecretKeyRotationPlan(
        current_value=new_current,
        previous_value=new_previous,
        retired_version=key_ring.current.version,
        new_version=new_version,
        previous_key_count=len(previous_entries),
    )


def parse_secret_key_ring_from_env(
    env: object | None,
) -> SecretKeyRing | None:
    """Create a key ring from environment-style settings.

    Returns ``None`` when neither current nor previous key material is configured.
    """

    if env is None:
        return None

    current = _normalise_environment_value(
        environment_get(env, ENV_WYBRA_SECRET_KEY_CURRENT)
    )
    previous = _normalise_environment_value(
        environment_get(env, ENV_WYBRA_SECRET_KEYS_PREVIOUS)
    )

    if current is None and previous is None:
        return None

    return parse_secret_key_bundle(current=current, previous=previous)


def parse_secret_key_ring_from_secrets(
    secrets: SecretKeySecretSource | None,
    *,
    source: SecretSource | str | None,
    current_key: str = ENV_WYBRA_SECRET_KEY_CURRENT,
    previous_keys: str | None = None,
) -> SecretKeyRing | None:
    """Create a key ring from a configured runtime secrets source.

    Resolved key values must use the same ``version:base64-key:checksum`` text
    entry format as environment-backed key material.

    Returns ``None`` when no source or secrets capability is available so callers
    can use the environment-backed key loader.
    """

    if secrets is None or source is None:
        return None

    source_name = normalise_secret_source(source)
    current = _resolve_required_secret_key(
        secrets,
        source=source_name,
        key=current_key,
        name="current secret key",
    )
    previous = _resolve_optional_secret_key(
        secrets,
        source=source_name,
        key=previous_keys,
        name="previous secret keys",
    )
    return parse_secret_key_bundle(current=current, previous=previous)


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


@dataclass(frozen=True, slots=True)
class SecretEnvelope:
    """Encrypted secret envelope value passed across service boundaries."""

    value: str

    @classmethod
    def from_plaintext(
        cls,
        value: str,
        *,
        service: SecretEnvelopeService,
        required: bool = True,
    ) -> SecretEnvelope:
        encrypted = service.encrypt(value, required=required)
        if _decode_envelope(encrypted) is None:
            raise SecretDataError(
                "SecretEnvelope requires an encrypted secret envelope value."
            )

        return cls(encrypted)

    def decrypt(
        self,
        *,
        service: SecretEnvelopeService,
        required: bool = True,
    ) -> tuple[str, str]:
        return service.decrypt(self.value, required=required)

    def __str__(self) -> str:
        return self.value


def _encode_envelope(version: str, token: str) -> str:
    return ENVELOPE_SEPARATOR.join((ENVELOPE_PREFIX, version, token))


def _decode_envelope(value: str) -> tuple[str, str] | None:
    if not value.startswith(ENVELOPE_PREFIX + ENVELOPE_SEPARATOR):
        return None

    parts = value.split(ENVELOPE_SEPARATOR, 2)
    if len(parts) != 3:
        return None

    prefix, version, encrypted = parts
    if prefix != ENVELOPE_PREFIX:
        return None

    if not version or not encrypted:
        return None

    return version, encrypted


def _encode_verifier(version: str, digest: str) -> str:
    return ENVELOPE_SEPARATOR.join((VERIFIER_PREFIX, version, digest))


def _decode_verifier(value: str) -> tuple[str, str] | None:
    if not value.startswith(VERIFIER_PREFIX + ENVELOPE_SEPARATOR):
        return None

    parts = value.split(ENVELOPE_SEPARATOR, 2)
    if len(parts) != 3:
        return None

    prefix, version, digest = parts
    if prefix != VERIFIER_PREFIX or not version or not digest:
        return None

    return version, digest


def _secret_key_reference(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise SecretMaterialMissingError(f"{name} reference must be a non-blank string.")


def _resolve_secret_key(
    secrets: SecretKeySecretSource,
    *,
    source: SecretSource,
    key: str | None,
    name: str,
    required: bool,
) -> str | None:
    key_reference = _secret_key_reference(key, name=name)
    if key_reference is None:
        return None
    try:
        return secrets.resolve(source, key_reference).reveal()
    except MissingSecretError as exc:
        if not required:
            return None
        raise SecretMaterialMissingError(
            f"{name} is not configured in the selected secrets source: {exc}"
        ) from exc
    except SecretsError as exc:
        raise SecretMaterialMissingError(
            f"{name} could not be resolved from the selected secrets source: {exc}"
        ) from exc


def _resolve_required_secret_key(
    secrets: SecretKeySecretSource,
    *,
    source: SecretSource,
    key: str,
    name: str,
) -> str:
    value = _resolve_secret_key(
        secrets,
        source=source,
        key=key,
        name=name,
        required=True,
    )
    assert value is not None
    return value


def _resolve_optional_secret_key(
    secrets: SecretKeySecretSource,
    *,
    source: SecretSource,
    key: str | None,
    name: str,
) -> str | None:
    return _resolve_secret_key(
        secrets,
        source=source,
        key=key,
        name=name,
        required=False,
    )


def _verifier_digest(*, key: bytes, context: str, value: str) -> str:
    payload = f"{context}\0{value}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


class SecretEnvelopeService:
    """Encrypt and decrypt secret values with versioned key material."""

    def __init__(self, key_loader: Callable[[], SecretKeyRing | None]):
        self._key_loader = key_loader
        self._key_ring: SecretKeyRing | None = None
        self._key_ring_loaded = False

    @classmethod
    def from_env(cls, env: object | None) -> SecretEnvelopeService:
        """Create the production runtime service from environment-style key settings."""
        return cls(lambda: parse_secret_key_ring_from_env(env))

    @classmethod
    def from_secrets(
        cls,
        secrets: SecretKeySecretSource | None,
        *,
        source: SecretSource | str | None,
        current_key: str = ENV_WYBRA_SECRET_KEY_CURRENT,
        previous_keys: str | None = None,
    ) -> SecretEnvelopeService:
        """Create a service from a configured runtime secrets source."""
        return cls(
            lambda: parse_secret_key_ring_from_secrets(
                secrets,
                source=source,
                current_key=current_key,
                previous_keys=previous_keys,
            )
        )

    @classmethod
    def from_key_bundle(
        cls,
        current: str | None,
        previous: str | None = None,
    ) -> SecretEnvelopeService:
        """Create a service from explicit key entries.

        Runtime storage paths should prefer :meth:`from_env` so deployment key
        material comes from configured Wybra secret environment values. This
        constructor is for already-resolved configuration and test injection,
        where callers deliberately provide concrete key entries.
        """
        if current is None and previous is None:
            return cls(lambda: None)

        return cls(lambda: parse_secret_key_bundle(current=current, previous=previous))

    @classmethod
    def for_testing(cls, *, version: str = "test") -> SecretEnvelopeService:
        """Create a test-only service with generated key material.

        This keeps the ``version:base64-key:checksum`` wire format owned by the
        crypto module instead of scattering manual test bundle construction
        across consuming repositories.
        """
        key = Fernet.generate_key()
        encoded_key = key.decode("ascii")
        raw_key = base64.urlsafe_b64decode(encoded_key)
        return cls.from_key_bundle(f"{version}:{encoded_key}:{_checksum(raw_key)}")

    def encrypt_required(self, value: str) -> str:
        return self.encrypt(value, required=True)

    def decrypt_required(self, value: str) -> tuple[str, str]:
        return self.decrypt(value, required=True)

    def current_version_required(self) -> str:
        key_ring = self._get_key_ring(required=True)
        if key_ring is None:  # pragma: no cover - required branch raises first
            raise SecretMaterialMissingError(
                "Secret material is required but no keys are configured."
            )
        return key_ring.current.version

    def create_verifier_required(self, value: str, *, context: str) -> str:
        key_ring = self._get_key_ring(required=True)
        if key_ring is None:  # pragma: no cover - required branch raises first
            raise SecretMaterialMissingError(
                "Secret material is required but no keys are configured."
            )

        digest = _verifier_digest(
            key=key_ring.current.key,
            context=context,
            value=value,
        )
        return _encode_verifier(key_ring.current.version, digest)

    def verify_verifier_required(
        self,
        value: str,
        verifier: str,
        *,
        context: str,
    ) -> bool:
        envelope = _decode_verifier(verifier)
        if envelope is None:
            raise SecretDataError("Secret verifier value is invalid or malformed.")

        key_ring = self._get_key_ring(required=True)
        if key_ring is None:  # pragma: no cover - required branch raises first
            raise SecretMaterialMissingError(
                "Secret material is required but no keys are configured."
            )

        version, expected_digest = envelope
        key = next(
            (
                candidate.key
                for candidate in key_ring.keys
                if candidate.version == version
            ),
            None,
        )
        if key is None:
            raise SecretVersionError(f"Unknown secret version: {version}.")

        actual_digest = _verifier_digest(key=key, context=context, value=value)
        return hmac.compare_digest(actual_digest, expected_digest)

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
            if value == ENVELOPE_PREFIX or value.startswith(
                ENVELOPE_PREFIX + ENVELOPE_SEPARATOR
            ):
                raise SecretDataError(
                    "Encrypted secret envelope is invalid or malformed."
                )
            return value, PLAIN_TEXT_VERSION

        key_ring = self._get_key_ring(required=required)
        if key_ring is None:
            return value, envelope[0]

        version, token = envelope
        fernet = key_ring.fernet_for(version)
        try:
            encrypted = token.encode("ascii")
            return fernet.decrypt(encrypted).decode("utf-8"), version
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise SecretDataError(
                "Encrypted secret value is invalid or corrupt."
            ) from exc

    def refresh_key_ring(self) -> None:
        """Drop cached key-ring state so key material reloads on the next operation."""

        self._key_ring = None
        self._key_ring_loaded = False


__all__ = [
    "ENVELOPE_PREFIX",
    "VERIFIER_PREFIX",
    "PLAIN_TEXT_VERSION",
    "ENV_WYBRA_SECRET_KEY_CURRENT",
    "ENV_WYBRA_SECRET_KEYS_PREVIOUS",
    "SecretDataError",
    "SecretEnvelope",
    "SecretMaterialMissingError",
    "SecretVersionError",
    "SecretEnvelopeService",
    "SecretKeySecretSource",
    "SecretKey",
    "SecretKeyRing",
    "SecretKeyRotationPlan",
    "generate_secret_key_entry",
    "parse_secret_key_bundle",
    "parse_secret_key_ring_from_env",
    "parse_secret_key_ring_from_secrets",
    "parse_secret_key_entry",
    "plan_secret_key_rotation",
]
