from __future__ import annotations


class StreamsError(RuntimeError):
    """Base error for live stream operations."""


class StreamsProviderError(StreamsError):
    """Raised when the selected streams provider cannot complete an operation."""


class StreamSubscriptionClosedError(StreamsError):
    """Raised when a closed subscription is read."""


__all__ = (
    "StreamSubscriptionClosedError",
    "StreamsError",
    "StreamsProviderError",
)
