from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from wybra.core.exceptions import ConfigurationError
from wybra.streams.contracts import (
    StreamSubscription,
    stream_resource_digest,
    validate_stream_payload,
    validate_subscription_timeout,
)
from wybra.streams.exceptions import StreamsProviderError, StreamSubscriptionClosedError

logger = logging.getLogger(__name__)
type SubscriptionCloser = Callable[[], Awaitable[None]]


@dataclass(slots=True, eq=False)
class RedisStreamSubscription(StreamSubscription):
    _handle: Any = field(repr=False)
    _channel: str = field(repr=False)
    _close_callback: SubscriptionCloser = field(repr=False)
    _closed: bool = field(default=False, init=False)
    _released: bool = field(default=False, init=False)
    _close_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _read_idle: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _close_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _receive_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._read_idle.set()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.receive()
        except StreamSubscriptionClosedError as exc:
            raise StopAsyncIteration from exc

    async def receive(self, *, timeout: float | None = None) -> bytes:
        timeout = None if timeout is None else validate_subscription_timeout(timeout)
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        await self._acquire_receive_lock(deadline)
        try:
            self._require_open()
            while True:
                message = await self._read_or_close(_remaining_timeout(deadline))
                if message is not None:
                    return _message_payload(message, self._channel)
                if deadline is not None:
                    remaining = _remaining_timeout(deadline)
                    assert remaining is not None
                    if remaining <= 0:
                        raise TimeoutError("Redis streams receive timed out.")
                await asyncio.sleep(0)
        finally:
            self._receive_lock.release()

    async def close(self) -> None:
        async with self._close_lock:
            if self._released:
                return
            self._closed = True
            self._close_event.set()
            await self._read_idle.wait()
            await self._close_callback()
            self._released = True

    async def _acquire_receive_lock(self, deadline: float | None) -> None:
        if deadline is None:
            await self._receive_lock.acquire()
            return
        remaining = _remaining_timeout(deadline)
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("Redis streams receive timed out.")
        try:
            await asyncio.wait_for(self._receive_lock.acquire(), timeout=remaining)
        except TimeoutError:
            raise TimeoutError("Redis streams receive timed out.") from None

    async def _read_or_close(self, timeout: float | None) -> object:
        self._read_idle.clear()
        read = asyncio.create_task(
            self._handle.get_message(
                ignore_subscribe_messages=True,
                timeout=timeout,
            )
        )
        closed = asyncio.create_task(self._close_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (read, closed),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if read in done or read.done():
                try:
                    return await read
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    raise TimeoutError("Redis streams receive timed out.") from None
                except Exception as exc:
                    logger.warning(
                        "Redis streams receive failed (%s).",
                        type(exc).__name__,
                    )
                    raise StreamsProviderError("Redis streams receive failed.") from None
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
            if closed in done or closed.done():
                raise StreamSubscriptionClosedError("The stream subscription is closed.")
            raise TimeoutError("Redis streams receive timed out.")
        finally:
            for task in (read, closed):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(read, closed, return_exceptions=True)
            finally:
                self._read_idle.set()

    def _require_open(self) -> None:
        if self._closed:
            raise StreamSubscriptionClosedError("The stream subscription is closed.")


@dataclass(slots=True)
class RedisStreamsProvider:
    url: str = field(repr=False)
    namespace: str
    _client: Any = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _cleanup_complete: bool = field(default=False, init=False, repr=False)
    _subscriptions: set[RedisStreamSubscription] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _close_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    async def start(self) -> None:
        try:
            await self._client_for_use().ping()
        except asyncio.CancelledError:
            raise
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.warning("Redis streams startup failed (%s).", type(exc).__name__)
            raise StreamsProviderError("Redis streams provider startup failed.") from None

    async def publish(self, owner: str, topic: str, payload: bytes) -> None:
        channel = self._channel(owner, topic)
        payload = validate_stream_payload(payload)
        try:
            await self._client_for_use().publish(channel, payload)
        except asyncio.CancelledError:
            raise
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.warning("Redis streams publish failed (%s).", type(exc).__name__)
            raise StreamsProviderError("Redis streams publish failed.") from None

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription:
        channel = self._channel(owner, topic)
        handle: Any = None
        try:
            handle = self._client_for_use().pubsub()
            await handle.subscribe(channel)
            confirmation = await handle.get_message(
                ignore_subscribe_messages=False,
                timeout=1,
            )
            _validate_subscription_confirmation(confirmation, channel)

            subscription: RedisStreamSubscription

            async def close_subscription() -> None:
                await handle.aclose()
                async with self._lock:
                    self._subscriptions.discard(subscription)

            subscription = RedisStreamSubscription(handle, channel, close_subscription)
            async with self._lock:
                self._require_open()
                self._subscriptions.add(subscription)
            return subscription
        except asyncio.CancelledError:
            if handle is not None:
                await _close_handle(handle)
            raise
        except ConfigurationError, StreamsProviderError:
            if handle is not None:
                await _close_handle(handle)
            raise
        except Exception as exc:
            if handle is not None:
                await _close_handle(handle)
            logger.warning("Redis streams subscribe failed (%s).", type(exc).__name__)
            raise StreamsProviderError("Redis streams subscribe failed.") from None

    async def close(self) -> None:
        async with self._close_lock:
            if self._cleanup_complete:
                return
            self._closed = True
            async with self._lock:
                subscriptions = tuple(self._subscriptions)
            errors: list[Exception] = []
            cancellation: asyncio.CancelledError | None = None
            for subscription in subscriptions:
                try:
                    await subscription.close()
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._subscriptions.discard(subscription)
            client = self._client
            if client is not None:
                try:
                    await client.aclose()
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._client = None
            if cancellation is not None:
                if errors:
                    cancellation.add_note(f"{len(errors)} Redis streams cleanup error(s) also occurred.")
                raise cancellation
            if errors:
                raise ExceptionGroup("Redis streams provider cleanup failed.", errors)
            self._cleanup_complete = True

    def _client_for_use(self) -> Any:
        self._require_open()
        if self._client is None:
            try:
                redis = importlib.import_module("redis.asyncio")
            except ImportError as exc:
                raise ConfigurationError(
                    "Redis streams backend requires an optional dependency. Install wybra[streams]."
                ) from exc
            self._client = redis.Redis.from_url(self.url, decode_responses=False)
        return self._client

    def _channel(self, owner: str, topic: str) -> str:
        digest = stream_resource_digest(owner, topic)
        return f"wybra:streams:{self.namespace}:{digest}"

    def _require_open(self) -> None:
        if self._closed:
            raise StreamsProviderError("The Redis streams provider is closed.")


async def _close_handle(handle: Any) -> None:
    try:
        await handle.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Redis streams subscription cleanup failed (%s).",
            type(exc).__name__,
        )


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - asyncio.get_running_loop().time())


def _message_payload(value: object, channel: str) -> bytes:
    if not isinstance(value, Mapping):
        raise StreamsProviderError("Redis streams receive returned invalid state.")
    if (
        value.get("type") not in ("message", b"message")
        or value.get("channel") not in (channel, channel.encode())
        or not isinstance(value.get("data"), bytes)
    ):
        raise StreamsProviderError("Redis streams receive returned invalid state.")
    return validate_stream_payload(value["data"])


def _validate_subscription_confirmation(value: object, channel: str) -> None:
    if not isinstance(value, Mapping):
        raise StreamsProviderError("Redis streams subscribe returned invalid state.")
    if value.get("type") not in ("subscribe", b"subscribe") or value.get("channel") not in (channel, channel.encode()):
        raise StreamsProviderError("Redis streams subscribe returned invalid state.")


__all__ = ("RedisStreamSubscription", "RedisStreamsProvider")
