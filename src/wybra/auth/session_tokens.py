from __future__ import annotations

from secrets import token_urlsafe
from typing import Final

SESSION_TOKEN_NBYTES: Final = 32
SESSION_TOKEN_MAX_LENGTH: Final = 128


def generate_session_token() -> str:
    return token_urlsafe(SESSION_TOKEN_NBYTES)
