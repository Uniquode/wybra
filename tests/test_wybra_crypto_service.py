import base64
import zlib

import pytest
from cryptography.fernet import Fernet

from wybra.services.crypto import (
    ENV_WYBRA_SECRET_KEY,
    ENVELOPE_PREFIX,
    PLAIN_TEXT_VERSION,
    VERIFIER_PREFIX,
    SecretDataError,
    SecretEnvelope,
    SecretEnvelopeService,
    SecretKeyRing,
    SecretMaterialMissingError,
    SecretVersionError,
    generate_secret_key_entry,
    parse_secret_key_bundle,
    parse_secret_key_entry,
    parse_secret_key_ring_from_env,
    parse_secret_key_ring_from_secrets,
    plan_secret_key_rotation,
)
from wybra.services.secrets import MissingSecretError, SecretValue


def _key_entry(version: str, key: bytes) -> str:
    encoded_key = key.decode("ascii")
    checksum = f"{(zlib.crc32(base64.urlsafe_b64decode(encoded_key)) & 0xFFFFFFFF):08x}"
    return f"{version}:{encoded_key}:{checksum}"


def _key_bundle_key() -> bytes:
    return Fernet.generate_key()


def _service_with_keys(
    *,
    current: bytes,
    previous: bytes | None = None,
) -> SecretEnvelopeService:
    current_entry = _key_entry("current", current)
    previous_entry = _key_entry("previous", previous) if previous is not None else None
    return SecretEnvelopeService.from_key_bundle(current_entry, previous=previous_entry)


class FakeSecretsCapability:
    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})
        self.requests: list[tuple[str, str]] = []

    def resolve(self, source: str, key: str) -> SecretValue:
        self.requests.append((source, key))
        try:
            return SecretValue(self.values[(source, key)], source=source, key=key)
        except KeyError as exc:
            raise MissingSecretError(source=source, key=key) from exc


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


def test_parse_secret_key_bundle_accepts_current_and_previous_keys() -> None:
    current = _key_bundle_key()
    previous = _key_bundle_key()

    key_ring = parse_secret_key_bundle(
        current=_key_entry("v2", current),
        previous=_key_entry("v1", previous),
    )

    assert key_ring.current.version == "v2"
    assert {key.version for key in key_ring.keys} == {"v2", "v1"}


def test_parse_secret_key_bundle_rejects_duplicate_versions() -> None:
    key = _key_bundle_key()

    with pytest.raises(SecretDataError, match="unique"):
        parse_secret_key_bundle(
            current=_key_entry("v1", key),
            previous=_key_entry("v1", _key_bundle_key()),
        )


def test_generate_secret_key_entry_creates_parseable_unique_key() -> None:
    current = _key_entry("current", _key_bundle_key())
    previous = _key_entry("previous", _key_bundle_key())

    generated = generate_secret_key_entry(existing_versions={"current", "previous"})

    version, key = parse_secret_key_entry(generated)
    assert version not in {"current", "previous"}
    assert len(key) == 32
    parse_secret_key_bundle(current=generated, previous=f"{current},{previous}")


def test_generate_secret_key_entry_rejects_duplicate_explicit_version() -> None:
    with pytest.raises(SecretDataError, match="unique"):
        generate_secret_key_entry(
            version="current",
            existing_versions={"current"},
        )


def test_plan_secret_key_rotation_prepends_retired_current_key() -> None:
    current = _key_entry("current", _key_bundle_key())
    previous = _key_entry("previous", _key_bundle_key())

    plan = plan_secret_key_rotation(current=current, previous=previous)

    assert plan.retired_version == "current"
    assert plan.new_version not in {"current", "previous"}
    assert plan.previous_key_count == 2
    assert plan.previous_value == f"{current},{previous}"
    parse_secret_key_bundle(current=plan.current_value, previous=plan.previous_value)


def test_plan_secret_key_rotation_initialises_missing_previous_value() -> None:
    current = _key_entry("current", _key_bundle_key())

    plan = plan_secret_key_rotation(current=current, previous=None)

    assert plan.retired_version == "current"
    assert plan.previous_key_count == 1
    assert plan.previous_value == current


def test_plan_secret_key_rotation_rejects_invalid_inputs_before_writes() -> None:
    current = _key_entry("current", _key_bundle_key())

    with pytest.raises(SecretMaterialMissingError):
        plan_secret_key_rotation(current=None, previous=None)

    with pytest.raises(SecretDataError, match="format"):
        plan_secret_key_rotation(current="not-a-key-entry", previous=None)

    with pytest.raises(SecretDataError, match="comma-separated"):
        plan_secret_key_rotation(current=current, previous=",")

    with pytest.raises(SecretDataError, match="unique"):
        plan_secret_key_rotation(current=current, previous=current)


def test_secret_key_rotation_plan_repr_redacts_key_material() -> None:
    current = _key_entry("current", _key_bundle_key())
    plan = plan_secret_key_rotation(current=current, previous=None)

    rendered = repr(plan)

    assert plan.current_value not in rendered
    assert plan.previous_value not in rendered
    assert current not in rendered
    assert plan.new_version in rendered


def test_parse_secret_key_ring_from_env_returns_none_if_unset() -> None:
    assert parse_secret_key_ring_from_env({}) is None


def test_parse_secret_key_ring_from_env_ignores_blank_values() -> None:
    assert (
        parse_secret_key_ring_from_env(
            {
                ENV_WYBRA_SECRET_KEY: "   ",
            }
        )
        is None
    )


def test_parse_secret_key_ring_from_secrets_reads_current_and_previous_keys() -> None:
    current = _key_entry("current", _key_bundle_key())
    previous = _key_entry("previous", _key_bundle_key())
    secrets = FakeSecretsCapability(
        {
            ("keychain", "current-key"): current,
            ("keychain", "previous-keys"): previous,
        }
    )

    key_ring = parse_secret_key_ring_from_secrets(
        secrets,
        source="keychain",
        current_key="current-key",
        previous_keys="previous-keys",
    )

    assert key_ring is not None
    assert key_ring.current.version == "current"
    assert {key.version for key in key_ring.keys} == {"current", "previous"}
    assert secrets.requests == [
        ("keychain", "current-key"),
        ("keychain", "previous-keys"),
    ]


def test_parse_secret_key_ring_from_secrets_treats_missing_previous_as_optional() -> (
    None
):
    current = _key_entry("current", _key_bundle_key())
    secrets = FakeSecretsCapability({("vault", "current-key"): current})

    key_ring = parse_secret_key_ring_from_secrets(
        secrets,
        source="vault",
        current_key="current-key",
        previous_keys="previous-keys",
    )

    assert key_ring is not None
    assert key_ring.current.version == "current"
    assert [key.version for key in key_ring.keys] == ["current"]


def test_parse_secret_key_ring_from_secrets_returns_none_without_source() -> None:
    assert parse_secret_key_ring_from_secrets(None, source="keychain") is None
    assert (
        parse_secret_key_ring_from_secrets(FakeSecretsCapability(), source=None) is None
    )


def test_parse_secret_key_ring_from_secrets_rejects_missing_current() -> None:
    secrets = FakeSecretsCapability()

    with pytest.raises(
        SecretMaterialMissingError,
        match=(
            "current secret key is not configured in the selected secrets source: "
            "Missing secret: source=keychain, key=current-key"
        ),
    ):
        parse_secret_key_ring_from_secrets(
            secrets,
            source="keychain",
            current_key="current-key",
        )


def test_parse_secret_key_ring_from_secrets_rejects_blank_current_reference() -> None:
    secrets = FakeSecretsCapability()

    with pytest.raises(
        SecretMaterialMissingError,
        match="current secret key reference must be a non-blank string",
    ):
        parse_secret_key_ring_from_secrets(
            secrets,
            source="keychain",
            current_key=" ",
        )


def test_secret_envelope_service_loads_secrets_backed_keys_lazily() -> None:
    current = _key_entry("current", _key_bundle_key())
    secrets = FakeSecretsCapability({("environment", "SYSTEM_SECRET_KEY"): current})
    service = SecretEnvelopeService.from_secrets(
        secrets,
        source="environment",
        current_key="SYSTEM_SECRET_KEY",
    )

    assert secrets.requests == []

    encrypted = service.encrypt_required("secret")

    assert encrypted.startswith(f"{ENVELOPE_PREFIX}|current|")
    assert secrets.requests == [("environment", "SYSTEM_SECRET_KEY")]


def test_encrypt_and_decrypt_current_and_previous_versions() -> None:
    current = _key_bundle_key()
    previous = _key_bundle_key()

    previous_service = SecretEnvelopeService.from_key_bundle(
        _key_entry("previous", previous)
    )
    previous_blob = previous_service.encrypt("previous-token")

    service = _service_with_keys(current=current, previous=previous)
    current_blob = service.encrypt("current-token")
    previous_plaintext, previous_version = service.decrypt(previous_blob)
    current_plaintext, current_version = service.decrypt(current_blob)

    assert current_version == "current"
    assert current_plaintext == "current-token"
    assert current_blob.startswith(f"{ENVELOPE_PREFIX}|current|")
    assert previous_version == "previous"
    assert previous_plaintext == "previous-token"


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


def test_secret_envelope_rejects_optional_plaintext_fallback() -> None:
    service = SecretEnvelopeService.from_env({})

    with pytest.raises(SecretDataError, match="requires an encrypted"):
        SecretEnvelope.from_plaintext(
            "provider-token",
            service=service,
            required=False,
        )


def test_create_and_verify_current_and_previous_verifiers() -> None:
    current = _key_bundle_key()
    previous = _key_bundle_key()

    previous_service = SecretEnvelopeService.from_key_bundle(
        _key_entry("previous", previous)
    )
    previous_verifier = previous_service.create_verifier_required(
        "recovery-code",
        context="test",
    )

    service = _service_with_keys(current=current, previous=previous)
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
        previous_verifier,
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
    env = {ENV_WYBRA_SECRET_KEY: _key_entry("v1", key_v1)}

    service = SecretEnvelopeService.from_env(env)

    encrypted_v1 = service.encrypt("token")
    envelope_prefix_v1 = f"{ENVELOPE_PREFIX}|v1|"
    assert encrypted_v1.startswith(envelope_prefix_v1)

    env[ENV_WYBRA_SECRET_KEY] = _key_entry("v2", key_v2)

    encrypted_stale = service.encrypt("token")
    assert encrypted_stale.startswith(envelope_prefix_v1)

    service.refresh_key_ring()
    encrypted_refreshed = service.encrypt("token")
    assert encrypted_refreshed.startswith(f"{ENVELOPE_PREFIX}|v2|")


def test_ring_from_secret_bundle_type_is_frozen_tuple() -> None:
    ring = parse_secret_key_bundle(current=_key_entry("v1", _key_bundle_key()))
    assert isinstance(ring, SecretKeyRing)
    assert len(ring.keys) == 1
