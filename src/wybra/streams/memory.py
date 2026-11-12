from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from wybra.streams.contracts import (
    StreamSubscription,
    validate_stream_payload,
    validate_stream_resource,
    validate_subscription_timeout,
)
from wybra.streams.exceptions import StreamsProviderError, StreamSubscriptionClosedError

type StreamKey = tuple[str, str]
type SubscriptionCloser = Callable[[], Awaitable[None]]
_CLOSED = object()


@dataclass(slots=True, eq=False)
class InMemoryStreamSubscription(StreamSubscription):
    _close_callback: SubscriptionCloser = field(repr=False)
    _messages: asyncio.Queue[bytes | object] = field(repr=False)
    _closed: bool = field(default=False, init=False)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.receive()
        except StreamSubscriptionClosedError as exc:
            raise StopAsyncIteration from exc

    async def receive(self, *, timeout: float | None = None) -> bytes:
        if self._closed:
            raise StreamSubscriptionClosedError("The stream subscription is closed.")
        if timeout is None:
            message = await self._messages.get()
        else:
            message = await asyncio.wait_for(
                self._messages.get(),
                timeout=validate_subscription_timeout(timeout),
            )
        if message is _CLOSED:
            raise StreamSubscriptionClosedError("The stream subscription is closed.")
        assert isinstance(message, bytes)
        return message

    async def close(self) -> None:
        if self._closed:
            return
        await self._close_callback()
        self._mark_closed()

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._messages.full():
            self._messages.get_nowait()
        self._messages.put_nowait(_CLOSED)


@dataclass(slots=True)
class InMemoryStreamsProvider:
    subscriber_buffer: int = 1_000
    max_topics: int = 1_000
    max_subscriptions: int = 10_000
    _subscriptions: dict[StreamKey, set[InMemoryStreamSubscription]] = field(
        default_factory=dict,
        init=False,
    )
    _closed: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.subscriber_buffer, "subscriber buffer"),
            (self.max_topics, "maximum topics"),
            (self.max_subscriptions, "maximum subscriptions"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label.capitalize()} must be a positive integer.")

    async def publish(self, owner: str, topic: str, payload: bytes) -> None:
        key = _stream_key(owner, topic)
        payload = validate_stream_payload(payload)
        async with self._lock:
            self._require_open()
            for subscription in tuple(self._subscriptions.get(key, ())):
                try:
                    subscription._messages.put_nowait(payload)
                except asyncio.QueueFull:
                    subscription._mark_closed()
                    self._remove_subscription(key, subscription)

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription:
        key = _stream_key(owner, topic)
        async with self._lock:
            self._require_open()
            if key not in self._subscriptions and len(self._subscriptions) >= self.max_topics:
                raise StreamsProviderError("The in-memory streams provider has reached its topic capacity.")
            if self._subscription_count() >= self.max_subscriptions:
                raise StreamsProviderError("The in-memory streams provider has reached its subscription capacity.")
            subscription: InMemoryStreamSubscription

            async def remove() -> None:
                async with self._lock:
                    self._remove_subscription(key, subscription)

            subscription = InMemoryStreamSubscription(
                remove,
                asyncio.Queue(maxsize=self.subscriber_buffer),
            )
            self._subscriptions.setdefault(key, set()).add(subscription)
            return subscription

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscriptions in self._subscriptions.values():
                for subscription in subscriptions:
                    subscription._mark_closed()
            self._subscriptions.clear()

    def _require_open(self) -> None:
        if self._closed:
            raise StreamsProviderError("The in-memory streams provider is closed.")

    def _remove_subscription(
        self,
        key: StreamKey,
        subscription: InMemoryStreamSubscription,
    ) -> None:
        subscriptions = self._subscriptions.get(key)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscriptions.pop(key, None)

    def _subscription_count(self) -> int:
        return sum(len(subscriptions) for subscriptions in self._subscriptions.values())


def _stream_key(owner: str, topic: str) -> StreamKey:
    return (
        validate_stream_resource(owner, label="stream owner"),
        validate_stream_resource(topic, label="stream topic"),
    )


__all__ = ("InMemoryStreamSubscription", "InMemoryStreamsProvider")
