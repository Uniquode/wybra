import base64
import zlib

import pytest
from cryptography.fernet import Fernet

from wevra.services.crypto import (
    ENV_WEVRA_SECRET_KEY_CURRENT,
    ENV_WEVRA_SECRET_KEY_LEGACY,
    ENVELOPE_PREFIX,
    PLAIN_TEXT_VERSION,
    VERIFIER_PREFIX,
    SecretDataError,
    SecretEnvelope,
    SecretEnvelopeService,
    SecretKeyRing,
    SecretMaterialMissingError,
    SecretVersionError,
    parse_secret_key_bundle,
    parse_secret_key_entry,
    parse_secret_key_ring_from_env,
)


def _key_entry(version: str, key: bytes) -> str:
    encoded_key = key.decode("ascii")
    checksum = f"{(zlib.crc32(base64.urlsafe_b64decode(encoded_key)) & 0xFFFFFFFF):08x}"
    return f"{version}:{encoded_key}:{checksum}"


def _key_bundle_key() -> bytes:
    return Fernet.generate_key()


def _service_with_keys(
    *,
    current: bytes,
    legacy: bytes | None = None,
) -> SecretEnvelopeService:
    current_entry = _key_entry("current", current)
    legacy_entry = _key_entry("legacy", legacy) if legacy is not None else None
    return SecretEnvelopeService.from_key_bundle(current_entry, legacy=legacy_entry)


def test_parse_secret_key_entry_parses_checksummed_keys() -> None:
    key = _key_bundle_key()
    entry = _key_entry("v1", key)

    version, parsed_key = parse_secret_key_entry(entry)

    assert version == "v1"
    assert parsed_key == base64.urlsafe_b64decode(key.decode("ascii"))


def test_parse_secret_key_entry_rejects_invalid_checksum() -> None:
    key = _key_bundle_key()
    actual_checksum = f"{
        (zlib.crc32(base64.urlsafe_b64decode(key.decode('ascii'))) & 0xFFFFFFFF):08x}"

    with pytest.raises(SecretDataError, match="checksum"):
        parse_secret_key_entry(f"v1:{key.decode('ascii')}:00000000")

    with pytest.raises(
        SecretDataError,
        match=f"expected {actual_checksum}, got 00000000",
    ):
        parse_secret_key_entry(f"v1:{key.decode('ascii')}:00000000")


def test_parse_secret_key_bundle_rejects_missing_current() -> None:
    with pytest.raises(SecretMaterialMissingError):
        parse_secret_key_bundle(current=None)


def test_parse_secret_key_bundle_accepts_current_and_legacy_keys() -> None:
    current = _key_bundle_key()
    legacy = _key_bundle_key()

    key_ring = parse_secret_key_bundle(
        current=_key_entry("v2", current),
        legacy=_key_entry("v1", legacy),
    )

    assert key_ring.current.version == "v2"
    assert {key.version for key in key_ring.keys} == {"v2", "v1"}


def test_parse_secret_key_bundle_rejects_duplicate_versions() -> None:
    key = _key_bundle_key()

    with pytest.raises(SecretDataError, match="unique"):
        parse_secret_key_bundle(
            current=_key_entry("v1", key),
            legacy=_key_entry("v1", _key_bundle_key()),
        )


def test_parse_secret_key_ring_from_env_returns_none_if_unset() -> None:
    assert parse_secret_key_ring_from_env({}) is None


def test_parse_secret_key_ring_from_env_ignores_blank_values() -> None:
    assert (
        parse_secret_key_ring_from_env(
            {
                ENV_WEVRA_SECRET_KEY_CURRENT: "   ",
            }
        )
        is None
    )


def test_parse_secret_key_ring_from_env_rejects_legacy_without_current() -> None:
    with pytest.raises(SecretMaterialMissingError):
        parse_secret_key_ring_from_env(
            {
                ENV_WEVRA_SECRET_KEY_LEGACY: _key_entry(
                    "legacy",
                    _key_bundle_key(),
                ),
            }
        )


def test_encrypt_and_decrypt_current_and_legacy_versions() -> None:
    current = _key_bundle_key()
    legacy = _key_bundle_key()

    legacy_service = SecretEnvelopeService.from_key_bundle(_key_entry("legacy", legacy))
    legacy_blob = legacy_service.encrypt("legacy-token")

    service = _service_with_keys(current=current, legacy=legacy)
    current_blob = service.encrypt("current-token")
    legacy_plaintext, legacy_version = service.decrypt(legacy_blob)
    current_plaintext, current_version = service.decrypt(current_blob)

    assert current_version == "current"
    assert current_plaintext == "current-token"
    assert current_blob.startswith(f"{ENVELOPE_PREFIX}|current|")
    assert legacy_version == "legacy"
    assert legacy_plaintext == "legacy-token"


def test_secret_envelope_wraps_encrypted_values_without_hiding_service() -> None:
    service = SecretEnvelopeService.for_testing(version="v1")

    envelope = SecretEnvelope.from_plaintext("provider-token", service=service)
    plaintext, version = envelope.decrypt(service=service)

    assert envelope.value.startswith(f"{ENVELOPE_PREFIX}|v1|")
    assert str(envelope) == envelope.value
    assert plaintext == "provider-token"
    assert version == "v1"


def test_secret_envelope_required_operations_reject_missing_keys() -> None:
    service = SecretEnvelopeService.from_env({})

    with pytest.raises(SecretMaterialMissingError, match="no keys"):
        SecretEnvelope.from_plaintext("provider-token", service=service)

    with pytest.raises(SecretMaterialMissingError, match="no keys"):
        SecretEnvelope(f"{ENVELOPE_PREFIX}|v1|token").decrypt(service=service)


def test_create_and_verify_current_and_legacy_verifiers() -> None:
    current = _key_bundle_key()
    legacy = _key_bundle_key()

    legacy_service = SecretEnvelopeService.from_key_bundle(_key_entry("legacy", legacy))
    legacy_verifier = legacy_service.create_verifier_required(
        "recovery-code",
        context="test",
    )

    service = _service_with_keys(current=current, legacy=legacy)
    current_verifier = service.create_verifier_required(
        "recovery-code",
        context="test",
    )

    assert current_verifier.startswith(f"{VERIFIER_PREFIX}|current|")
    assert service.verify_verifier_required(
        "recovery-code",
        current_verifier,
        context="test",
    )
    assert service.verify_verifier_required(
        "recovery-code",
        legacy_verifier,
        context="test",
    )
    assert not service.verify_verifier_required(
        "wrong-code",
        current_verifier,
        context="test",
    )


def test_for_testing_creates_required_secret_service() -> None:
    service = SecretEnvelopeService.for_testing()

    encrypted = service.encrypt_required("secret")

    assert encrypted.startswith(f"{ENVELOPE_PREFIX}|test|")
    assert service.decrypt_required(encrypted) == ("secret", "test")


def test_encrypt_without_required_keys_returns_plaintext() -> None:
    service = SecretEnvelopeService.from_env({})

    assert service.encrypt("secret") == "secret"
    assert service.decrypt("secret") == ("secret", PLAIN_TEXT_VERSION)


def test_encrypt_required_helper_rejects_missing_keys() -> None:
    service = SecretEnvelopeService.from_env({})

    with pytest.raises(SecretMaterialMissingError, match="no keys"):
        service.encrypt_required("secret")


def test_from_key_bundle_allows_no_configuration() -> None:
    service = SecretEnvelopeService.from_key_bundle(None)

    assert service.decrypt("secret") == ("secret", PLAIN_TEXT_VERSION)
    assert service.decrypt("plaintext") == ("plaintext", PLAIN_TEXT_VERSION)


def test_decrypt_rejects_malformed_envelope_values() -> None:
    service = SecretEnvelopeService.from_key_bundle(_key_entry("v1", _key_bundle_key()))

    with pytest.raises(SecretDataError, match="invalid or malformed"):
        service.decrypt(f"{ENVELOPE_PREFIX}|v1")

    with pytest.raises(SecretDataError, match="invalid or malformed"):
        service.decrypt(f"{ENVELOPE_PREFIX}")


def test_decrypt_rejects_non_ascii_encrypted_payload() -> None:
    key = _key_bundle_key()
    service = SecretEnvelopeService.from_key_bundle(_key_entry("v1", key))

    with pytest.raises(SecretDataError, match="invalid or corrupt"):
        service.decrypt(f"{ENVELOPE_PREFIX}|v1|not-base64-🍑")


def test_encrypt_rejects_required_missing_keys() -> None:
    service = SecretEnvelopeService.from_env({})

    with pytest.raises(SecretMaterialMissingError, match="no keys"):
        service.encrypt("secret", required=True)


def test_decrypt_rejects_missing_keys_when_required() -> None:
    service = SecretEnvelopeService.from_key_bundle(_key_entry("v1", _key_bundle_key()))
    plaintext = "value"
    encrypted = service.encrypt(plaintext)

    optional_service = SecretEnvelopeService.from_env({})
    with pytest.raises(SecretMaterialMissingError, match="no keys"):
        optional_service.decrypt(encrypted, required=True)


def test_secret_version_unknown_is_reported() -> None:
    service = SecretEnvelopeService.from_key_bundle(_key_entry("v1", _key_bundle_key()))
    encrypted = f"{ENVELOPE_PREFIX}|v-unknown|abc"

    with pytest.raises(SecretVersionError, match="Unknown secret version"):
        service.decrypt(encrypted)


def test_parse_secret_key_entry_rejects_reserved_plain_text_version() -> None:
    key = _key_bundle_key()
    with pytest.raises(SecretDataError, match="reserved plaintext"):
        parse_secret_key_entry(
            f"{PLAIN_TEXT_VERSION}:{key.decode('ascii')}:"
            f"{(zlib.crc32(base64.urlsafe_b64decode(key)) & 0xFFFFFFFF):08x}"
        )


def test_service_refreshes_cached_key_ring() -> None:
    key_v1 = _key_bundle_key()
    key_v2 = _key_bundle_key()
    env = {ENV_WEVRA_SECRET_KEY_CURRENT: _key_entry("v1", key_v1)}

    service = SecretEnvelopeService.from_env(env)

    encrypted_v1 = service.encrypt("token")
    envelope_prefix_v1 = f"{ENVELOPE_PREFIX}|v1|"
    assert encrypted_v1.startswith(envelope_prefix_v1)

    env[ENV_WEVRA_SECRET_KEY_CURRENT] = _key_entry("v2", key_v2)

    encrypted_stale = service.encrypt("token")
    assert encrypted_stale.startswith(envelope_prefix_v1)

    service.refresh_key_ring()
    encrypted_refreshed = service.encrypt("token")
    assert encrypted_refreshed.startswith(f"{ENVELOPE_PREFIX}|v2|")


def test_ring_from_secret_bundle_type_is_frozen_tuple() -> None:
    ring = parse_secret_key_bundle(current=_key_entry("v1", _key_bundle_key()))
    assert isinstance(ring, SecretKeyRing)
    assert len(ring.keys) == 1
