"""Recovery-code helper functions for one-time passphrase style fallback."""

from __future__ import annotations

import secrets
from typing import Final

from wevra.services.crypto import SecretEnvelopeService

RECOVERY_CODE_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECOVERY_CODE_COUNT: Final[int] = 10
RECOVERY_CODE_LENGTH: Final[int] = 10
RECOVERY_CODE_VERIFIER_CONTEXT: Final[str] = "wevra.auth.totp.recovery-code"


def generate_recovery_code(*, length: int = RECOVERY_CODE_LENGTH) -> str:
    if length <= 0:
        raise ValueError("Recovery code length must be positive.")

    return "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(length))


def generate_recovery_codes(*, count: int = RECOVERY_CODE_COUNT) -> tuple[str, ...]:
    if count <= 0:
        raise ValueError("Recovery code count must be positive.")

    return tuple(generate_recovery_code() for _ in range(count))


def create_recovery_code_verifier(
    code: str,
    secret_service: SecretEnvelopeService,
) -> str:
    return secret_service.create_verifier_required(
        code,
        context=RECOVERY_CODE_VERIFIER_CONTEXT,
    )


def verify_recovery_code(
    code: str,
    verifier: str,
    secret_service: SecretEnvelopeService,
) -> bool:
    return secret_service.verify_verifier_required(
        code,
        verifier,
        context=RECOVERY_CODE_VERIFIER_CONTEXT,
    )
