from __future__ import annotations

from collections.abc import AsyncIterator
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from wybra.streams.settings import StreamsConfigurationDiagnostic

MAX_STREAM_PAYLOAD_BYTES = 65_536
MAX_STREAM_RESOURCE_LENGTH = 256


@runtime_checkable
class StreamSubscription(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def receive(self, *, timeout: float | None = None) -> bytes: ...

    async def close(self) -> None: ...


@runtime_checkable
class StreamsCapability(Protocol):
    async def publish(self, owner: str, topic: str, payload: bytes) -> None: ...

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription: ...

    def diagnostics(self) -> StreamsConfigurationDiagnostic: ...

    async def close(self) -> None: ...


def validate_stream_resource(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label.capitalize()} must be a non-blank string without surrounding whitespace.")
    if len(value) > MAX_STREAM_RESOURCE_LENGTH:
        raise ValueError(f"{label.capitalize()} cannot exceed {MAX_STREAM_RESOURCE_LENGTH} characters.")
    return value


def validate_stream_payload(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError("Stream payloads must be bytes.")
    if len(value) > MAX_STREAM_PAYLOAD_BYTES:
        raise ValueError(f"Stream payloads cannot exceed {MAX_STREAM_PAYLOAD_BYTES} bytes.")
    return value


def validate_subscription_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Subscription timeout must be a positive finite number.")
    timeout = float(value)
    if timeout <= 0 or timeout == float("inf") or timeout != timeout:
        raise ValueError("Subscription timeout must be a positive finite number.")
    return timeout


def stream_resource_digest(owner: object, topic: object) -> str:
    """Return an unambiguous digest for one validated owner/topic pair."""
    owner_value = validate_stream_resource(owner, label="stream owner")
    topic_value = validate_stream_resource(topic, label="stream topic")
    owner_bytes = owner_value.encode()
    topic_bytes = topic_value.encode()
    encoded = b"".join(
        (
            len(owner_bytes).to_bytes(4, byteorder="big"),
            owner_bytes,
            len(topic_bytes).to_bytes(4, byteorder="big"),
            topic_bytes,
        )
    )
    return sha256(encoded).hexdigest()


__all__ = (
    "MAX_STREAM_PAYLOAD_BYTES",
    "MAX_STREAM_RESOURCE_LENGTH",
    "StreamSubscription",
    "StreamsCapability",
    "validate_stream_payload",
    "validate_stream_resource",
    "validate_subscription_timeout",
    "stream_resource_digest",
)
