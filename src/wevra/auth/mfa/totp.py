"""TOTP generation and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from secrets import token_bytes
from typing import Final
from urllib.parse import quote_plus

from wevra.auth.timestamps import current_timestamp

DEFAULT_TOTP_ALLOWED_DRIFT: Final[int] = 1
DEFAULT_TOTP_DIGITS: Final[int] = 6
DEFAULT_TOTP_PERIOD_SECONDS: Final[int] = 30
DEFAULT_TOTP_RECOVERY_WINDOW_SECONDS: Final[int] = 15 * 60
DEFAULT_TOTP_SECRET_BYTES: Final[int] = 20
SUPPORTED_TOTP_ALGORITHMS: Final[tuple[str, ...]] = ("SHA1",)
MAX_TOTP_ALLOWED_DRIFT: Final[int] = 10
MAX_TOTP_CODE_LENGTH: Final[int] = 9
MAX_TOTP_PERIOD_SECONDS: Final[int] = 120
MAX_TOTP_RECOVERY_WINDOW_SECONDS: Final[int] = 15 * 60


def generate_totp_secret(*, byte_count: int = DEFAULT_TOTP_SECRET_BYTES) -> str:
    """Generate a random Base32-encoded secret for a TOTP credential."""
    if byte_count <= 0:
        raise ValueError("TOTP secret byte count must be positive.")

    return _normalise_totp_secret(
        base64.b32encode(token_bytes(byte_count)).decode("ascii")
    )


def totp_auth_uri(
    *,
    account_name: str,
    secret: str,
    issuer: str,
    digits: int = DEFAULT_TOTP_DIGITS,
    period: int = DEFAULT_TOTP_PERIOD_SECONDS,
    algorithm: str = SUPPORTED_TOTP_ALGORITHMS[0],
) -> str:
    algorithm = _normalise_totp_algorithm(algorithm)
    return (
        f"otpauth://totp/{_uri_component(issuer)}:{_uri_component(account_name)}?"
        f"secret={_normalise_totp_secret(secret)}&issuer={_uri_component(issuer)}"
        f"&algorithm={algorithm}&digits={digits}&period={period}"
    )


def is_valid_totp_code(code: str, *, digits: int = DEFAULT_TOTP_DIGITS) -> bool:
    if len(code) != digits:
        return False

    return code.isdigit()


def generate_totp(
    secret: str,
    *,
    timestamp: float | None = None,
    digits: int = DEFAULT_TOTP_DIGITS,
    period: int = DEFAULT_TOTP_PERIOD_SECONDS,
) -> str:
    return _generate_counter_totp(
        secret,
        _current_counter(timestamp=_totp_timestamp_seconds(timestamp), period=period),
        digits=digits,
    )


def verify_totp(
    secret: str,
    submitted_code: str,
    *,
    timestamp: float | None = None,
    digits: int = DEFAULT_TOTP_DIGITS,
    period: int = DEFAULT_TOTP_PERIOD_SECONDS,
    allowed_drift: int = DEFAULT_TOTP_ALLOWED_DRIFT,
) -> tuple[bool, int | None]:
    if allowed_drift < 0:
        raise ValueError("TOTP allowed drift must be non-negative.")
    if allowed_drift > MAX_TOTP_ALLOWED_DRIFT:
        raise ValueError("TOTP allowed drift exceeds the maximum supported drift.")

    if not is_valid_totp_code(submitted_code, digits=digits):
        return False, None

    current_counter = _current_counter(
        timestamp=_totp_timestamp_seconds(timestamp),
        period=period,
    )
    for delta in range(-allowed_drift, allowed_drift + 1):
        candidate_counter = current_counter + delta
        if candidate_counter < 0:
            continue

        candidate = _generate_counter_totp(
            secret,
            candidate_counter,
            digits=digits,
        )
        if hmac.compare_digest(candidate, submitted_code):
            return True, candidate_counter

    return False, None


def totp_recovery_window_expiry_timestamp(
    *,
    timestamp: float | None = None,
    recovery_window_seconds: int = DEFAULT_TOTP_RECOVERY_WINDOW_SECONDS,
) -> float:
    return (timestamp if timestamp is not None else current_timestamp()) + (
        float(recovery_window_seconds)
    )


def _normalise_totp_secret(secret: str) -> str:
    return "".join(secret.strip().split()).replace("-", "").upper()


def _normalise_totp_algorithm(algorithm: str) -> str:
    normalised_algorithm = algorithm.strip().upper()
    if normalised_algorithm not in SUPPORTED_TOTP_ALGORITHMS:
        raise ValueError(
            "Unsupported TOTP algorithm. Supported values: "
            + ", ".join(SUPPORTED_TOTP_ALGORITHMS)
        )

    return normalised_algorithm


def _uri_component(value: str) -> str:
    return quote_plus(value)


def _current_counter(
    *,
    timestamp: int | None,
    period: int,
) -> int:
    if period <= 0:
        raise ValueError("TOTP period must be greater than zero.")

    current_time = int(current_timestamp()) if timestamp is None else timestamp
    return current_time // period


def _totp_timestamp_seconds(timestamp: float | None) -> int | None:
    if timestamp is None:
        return None

    return int(timestamp)


def _generate_counter_totp(
    secret: str,
    counter: int,
    *,
    digits: int = DEFAULT_TOTP_DIGITS,
) -> str:
    if digits < 6 or digits > MAX_TOTP_CODE_LENGTH:
        raise ValueError("TOTP digits must be between 6 and 9.")

    normalized_secret = _normalise_totp_secret(secret)
    padded_secret = normalized_secret + "=" * ((8 - len(normalized_secret) % 8) % 8)
    try:
        key = base64.b32decode(padded_secret, casefold=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid TOTP secret.") from exc

    counter_bytes = counter.to_bytes(8, "big", signed=False)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    mod = 10**digits
    return str(code % mod).zfill(digits)
