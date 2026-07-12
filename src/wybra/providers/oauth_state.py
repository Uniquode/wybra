from __future__ import annotations

import base64
import hashlib
import hmac
import json
from binascii import Error as BinasciiError
from collections.abc import Callable, Mapping
from typing import cast

from wybra.auth.timestamps import current_timestamp

_STATE_COOKIE_SEPARATOR = "."
type StateFactory[StateT] = Callable[[dict[object, object]], StateT | None]


def encode_signed_oauth_state(
    payload: Mapping[str, object],
    *,
    secret: str,
) -> str:
    encoded_payload = urlsafe_b64encode(
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return (
        f"{encoded_payload}{_STATE_COOKIE_SEPARATOR}"
        f"{_signature(encoded_payload, secret)}"
    )


def decode_signed_oauth_state[StateT](
    value: str,
    *,
    secret: str,
    state_factory: StateFactory[StateT],
    now: float | None = None,
) -> StateT | None:
    payload, separator, signature = value.partition(_STATE_COOKIE_SEPARATOR)
    if separator != _STATE_COOKIE_SEPARATOR or not payload or not signature:
        return None
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        return None

    try:
        raw_payload = json.loads(urlsafe_b64decode(payload).decode("utf-8"))
    except BinasciiError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(raw_payload, dict):
        return None

    state = state_factory(cast(dict[object, object], raw_payload))
    if state is None:
        return None
    expires_at = getattr(state, "expires_at", None)
    if not isinstance(expires_at, (int, float)):
        return None
    if (current_timestamp() if now is None else now) > float(expires_at):
        return None
    return state


def urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _signature(payload: str, secret: str) -> str:
    return urlsafe_b64encode(
        hmac.new(
            secret.encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


__all__ = (
    "StateFactory",
    "decode_signed_oauth_state",
    "encode_signed_oauth_state",
    "urlsafe_b64decode",
    "urlsafe_b64encode",
)
