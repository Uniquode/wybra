from __future__ import annotations

import re
from secrets import token_urlsafe
from typing import Final

from wybra.auth.timestamps import current_timestamp
from wybra.sessions.exceptions import SessionIdentifierError

SESSION_ID_PREFIX: Final = "s1"
SESSION_ID_RANDOM_BYTES: Final = 24
_SESSION_ID_RE: Final = re.compile(r"^s1_[0-9a-z]{10}_[A-Za-z0-9_-]{32,}$")
_BASE36_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"


def create_session_id(*, now: float | None = None) -> str:
    timestamp = int((current_timestamp() if now is None else now) * 1000)
    return f"{SESSION_ID_PREFIX}_{_base36(timestamp).zfill(10)}_{_random_token()}"


def validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise SessionIdentifierError("Session identifier is malformed or unsafe.")
    return value


def session_id_is_valid(value: object) -> bool:
    try:
        validate_session_id(value)
    except SessionIdentifierError:
        return False
    return True


def _random_token() -> str:
    return token_urlsafe(SESSION_ID_RANDOM_BYTES).rstrip("=")


def _base36(value: int) -> str:
    if value < 0:
        raise SessionIdentifierError("Session timestamp must be positive.")
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits))


__all__ = (
    "SESSION_ID_PREFIX",
    "create_session_id",
    "session_id_is_valid",
    "validate_session_id",
)
