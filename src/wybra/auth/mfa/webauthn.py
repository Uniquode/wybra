"""WebAuthn/passkey ceremony helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, WebAuthnException
from webauthn.helpers.options_to_json_dict import options_to_json_dict
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.challenges import (
    WEBAUTHN_ASSERTION_METHOD,
    AuthenticationAssertion,
)
from wybra.auth.mfa.storage import WebAuthnCredentialRecord
from wybra.auth.options import (
    PASSKEY_USER_VERIFICATION_REQUIRED,
    IdentityOptions,
)
from wybra.auth.timestamps import current_timestamp

WEBAUTHN_REGISTRATION_PURPOSE: Final = "passkey_registration"
WEBAUTHN_LOGIN_PURPOSE: Final = "passkey_login"
WEBAUTHN_CHALLENGE_FIELD: Final = "challenge"
WEBAUTHN_PURPOSE_FIELD: Final = "purpose"
WEBAUTHN_USER_HANDLE_FIELD: Final = "user_handle"
WEBAUTHN_RETURN_TO_FIELD: Final = "return_to"
WEBAUTHN_COUNTER_REGRESSION_REASON: Final = "counter_regression"
WEBAUTHN_INVALID_RESPONSE_REASON: Final = "invalid_response"


class WebAuthnCeremonyError(ValueError):
    """Raised when a WebAuthn ceremony cannot be completed."""

    def __init__(self, reason: str = WEBAUTHN_INVALID_RESPONSE_REASON):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WebAuthnOptionsResult:
    challenge: bytes
    public_key: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VerifiedWebAuthnRegistration:
    credential_id: str
    public_key: bytes
    sign_count: int
    user_verified: bool
    credential_device_type: str | None
    credential_backed_up: bool
    aaguid: str | None
    attestation_format: str | None


@dataclass(frozen=True, slots=True)
class VerifiedWebAuthnAuthentication:
    credential_id: str
    sign_count: int
    user_verified: bool
    credential_device_type: str | None
    credential_backed_up: bool


def passkeys_effectively_enabled(options: IdentityOptions) -> bool:
    return bool(
        options.passkey_enabled
        and options.passkey_rp_id.strip()
        and options.passkey_rp_name.strip()
        and options.passkey_allowed_origins
    )


def passkey_timeout_milliseconds(options: IdentityOptions) -> int:
    return int(options.passkey_timeout_seconds * 1000)


def passkey_registration_options(
    options: IdentityOptions,
    *,
    user_id: str,
    user_name: str,
    user_display_name: str | None = None,
    exclude_credentials: tuple[WebAuthnCredentialRecord, ...] = (),
) -> WebAuthnOptionsResult:
    parsed_user_id = parse_uuid(user_id)
    if parsed_user_id is None:
        raise WebAuthnCeremonyError()

    generated_options = generate_registration_options(
        rp_id=options.passkey_rp_id,
        rp_name=options.passkey_rp_name,
        user_name=user_name,
        user_id=parsed_user_id.bytes,
        user_display_name=user_display_name or user_name,
        timeout=passkey_timeout_milliseconds(options),
        attestation=_attestation(options),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=_resident_key(options),
            require_resident_key=(
                options.passkey_discoverable_credentials == "required"
            ),
            user_verification=_user_verification(options),
        ),
        exclude_credentials=[
            _credential_descriptor(credential) for credential in exclude_credentials
        ],
    )
    return WebAuthnOptionsResult(
        challenge=generated_options.challenge,
        public_key=options_to_json_dict(generated_options),
    )


def verify_passkey_registration(
    options: IdentityOptions,
    *,
    credential: Mapping[str, Any],
    expected_challenge: bytes,
) -> VerifiedWebAuthnRegistration:
    try:
        verified = verify_registration_response(
            credential=dict(credential),
            expected_challenge=expected_challenge,
            expected_rp_id=options.passkey_rp_id,
            expected_origin=list(options.passkey_allowed_origins),
            require_user_verification=_requires_user_verification(options),
        )
    except WebAuthnException as exc:
        raise WebAuthnCeremonyError() from exc

    return VerifiedWebAuthnRegistration(
        credential_id=credential_id_to_text(verified.credential_id),
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        user_verified=verified.user_verified,
        credential_device_type=_enum_value(verified.credential_device_type),
        credential_backed_up=verified.credential_backed_up,
        aaguid=verified.aaguid,
        attestation_format=_enum_value(verified.fmt),
    )


def passkey_authentication_options(
    options: IdentityOptions,
    *,
    allow_credentials: tuple[WebAuthnCredentialRecord, ...],
) -> WebAuthnOptionsResult:
    generated_options = generate_authentication_options(
        rp_id=options.passkey_rp_id,
        timeout=passkey_timeout_milliseconds(options),
        allow_credentials=[
            _credential_descriptor(credential) for credential in allow_credentials
        ],
        user_verification=_user_verification(options),
    )
    return WebAuthnOptionsResult(
        challenge=generated_options.challenge,
        public_key=options_to_json_dict(generated_options),
    )


def verify_passkey_authentication(
    options: IdentityOptions,
    *,
    credential: Mapping[str, Any],
    expected_challenge: bytes,
    stored_credential: WebAuthnCredentialRecord,
) -> VerifiedWebAuthnAuthentication:
    try:
        verified = verify_authentication_response(
            credential=dict(credential),
            expected_challenge=expected_challenge,
            expected_rp_id=options.passkey_rp_id,
            expected_origin=list(options.passkey_allowed_origins),
            credential_public_key=stored_credential.public_key,
            credential_current_sign_count=stored_credential.sign_count,
            require_user_verification=_requires_user_verification(options),
        )
    except InvalidAuthenticationResponse as exc:
        reason = (
            WEBAUTHN_COUNTER_REGRESSION_REASON
            if "sign count" in str(exc).lower()
            else WEBAUTHN_INVALID_RESPONSE_REASON
        )
        raise WebAuthnCeremonyError(reason) from exc
    except WebAuthnException as exc:
        raise WebAuthnCeremonyError() from exc

    return VerifiedWebAuthnAuthentication(
        credential_id=credential_id_to_text(verified.credential_id),
        sign_count=verified.new_sign_count,
        user_verified=verified.user_verified,
        credential_device_type=_enum_value(verified.credential_device_type),
        credential_backed_up=verified.credential_backed_up,
    )


def credential_id_to_text(credential_id: bytes) -> str:
    return bytes_to_base64url(credential_id)


def credential_id_to_bytes(credential_id: str) -> bytes:
    return base64url_to_bytes(credential_id)


def webauthn_challenge_metadata(
    *,
    purpose: str,
    challenge: bytes,
    user_handle: bytes | None = None,
    return_to: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        WEBAUTHN_PURPOSE_FIELD: purpose,
        WEBAUTHN_CHALLENGE_FIELD: bytes_to_base64url(challenge),
    }
    if user_handle is not None:
        metadata[WEBAUTHN_USER_HANDLE_FIELD] = bytes_to_base64url(user_handle)
    if return_to is not None:
        metadata[WEBAUTHN_RETURN_TO_FIELD] = return_to
    return metadata


def challenge_from_metadata(metadata: Mapping[str, object]) -> bytes | None:
    challenge = metadata.get(WEBAUTHN_CHALLENGE_FIELD)
    if not isinstance(challenge, str) or not challenge:
        return None
    try:
        return base64url_to_bytes(challenge)
    except ValueError:
        return None


def challenge_has_purpose(metadata: Mapping[str, object], purpose: str) -> bool:
    return metadata.get(WEBAUTHN_PURPOSE_FIELD) == purpose


def webauthn_assertion(
    user_id: str,
    *,
    ceremony_id: str,
    user_verified: bool,
    asserted_at: float | None = None,
) -> AuthenticationAssertion:
    return AuthenticationAssertion(
        user_id=user_id,
        method=WEBAUTHN_ASSERTION_METHOD,
        asserted_at=current_timestamp() if asserted_at is None else asserted_at,
        ceremony_id=ceremony_id,
        user_verified=user_verified,
    )


def _credential_descriptor(
    credential: WebAuthnCredentialRecord,
) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(
        id=credential_id_to_bytes(credential.credential_id),
        transports=[
            transport
            for transport_name in credential.transports
            if (transport := _transport(transport_name)) is not None
        ]
        or None,
    )


def _transport(value: str) -> AuthenticatorTransport | None:
    try:
        return AuthenticatorTransport(value)
    except ValueError:
        return None


def _user_verification(options: IdentityOptions) -> UserVerificationRequirement:
    return UserVerificationRequirement(options.passkey_user_verification)


def _requires_user_verification(options: IdentityOptions) -> bool:
    return options.passkey_user_verification == PASSKEY_USER_VERIFICATION_REQUIRED


def _attestation(options: IdentityOptions) -> AttestationConveyancePreference:
    return AttestationConveyancePreference(options.passkey_attestation)


def _resident_key(options: IdentityOptions) -> ResidentKeyRequirement:
    return ResidentKeyRequirement(options.passkey_discoverable_credentials)


def _enum_value(value: object) -> str | None:
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else None
