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
class NatsStreamSubscription(StreamSubscription):
    _handle: Any = field(repr=False)
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
            message = await self._read_or_close(_remaining_timeout(deadline))
            payload = getattr(message, "data", None)
            if not isinstance(payload, bytes):
                raise StreamsProviderError("NATS streams receive returned invalid state.")
            return validate_stream_payload(payload)
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
            raise TimeoutError("NATS streams receive timed out.")
        try:
            await asyncio.wait_for(self._receive_lock.acquire(), timeout=remaining)
        except TimeoutError:
            raise TimeoutError("NATS streams receive timed out.") from None

    async def _read_or_close(self, timeout: float | None) -> Any:
        self._read_idle.clear()
        read = asyncio.create_task(_next_message(self._handle, timeout))
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
                except Exception as exc:
                    if _is_timeout_error(exc):
                        raise TimeoutError("NATS streams receive timed out.") from None
                    logger.warning(
                        "NATS streams receive failed (%s).",
                        type(exc).__name__,
                    )
                    raise StreamsProviderError("NATS streams receive failed.") from None
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
            if closed in done or closed.done():
                raise StreamSubscriptionClosedError("The stream subscription is closed.")
            raise TimeoutError("NATS streams receive timed out.")
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
class NatsStreamsProvider:
    servers: tuple[str, ...] = field(repr=False)
    namespace: str
    connection_options: Mapping[str, str] = field(default_factory=dict, repr=False)
    _client: Any = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _cleanup_complete: bool = field(default=False, init=False, repr=False)
    _subscriptions: set[NatsStreamSubscription] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _start_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
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
        client = await self._client_for_use()
        try:
            await client.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("NATS streams startup failed (%s).", type(exc).__name__)
            raise StreamsProviderError("NATS streams provider startup failed.") from None

    async def publish(self, owner: str, topic: str, payload: bytes) -> None:
        subject = self._subject(owner, topic)
        payload = validate_stream_payload(payload)
        try:
            client = await self._client_for_use()
            await client.publish(subject, payload)
            await client.flush()
        except asyncio.CancelledError:
            raise
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.warning("NATS streams publish failed (%s).", type(exc).__name__)
            raise StreamsProviderError("NATS streams publish failed.") from None

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription:
        subject = self._subject(owner, topic)
        handle: Any = None
        try:
            client = await self._client_for_use()
            handle = await client.subscribe(subject)
            await client.flush()

            subscription: NatsStreamSubscription

            async def close_subscription() -> None:
                await handle.unsubscribe()
                async with self._lock:
                    self._subscriptions.discard(subscription)

            subscription = NatsStreamSubscription(handle, close_subscription)
            async with self._lock:
                self._require_open()
                self._subscriptions.add(subscription)
            return subscription
        except asyncio.CancelledError:
            if handle is not None:
                await _unsubscribe(handle)
            raise
        except ConfigurationError, StreamsProviderError:
            if handle is not None:
                await _unsubscribe(handle)
            raise
        except Exception as exc:
            if handle is not None:
                await _unsubscribe(handle)
            logger.warning("NATS streams subscribe failed (%s).", type(exc).__name__)
            raise StreamsProviderError("NATS streams subscribe failed.") from None

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
            if self._client is not None:
                try:
                    await self._client.close()
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._client = None
            if cancellation is not None:
                if errors:
                    cancellation.add_note(f"{len(errors)} NATS streams cleanup error(s) also occurred.")
                raise cancellation
            if errors:
                raise ExceptionGroup("NATS streams provider cleanup failed.", errors)
            self._cleanup_complete = True

    async def _client_for_use(self) -> Any:
        self._require_open()
        if self._client is not None:
            return self._client
        async with self._start_lock:
            self._require_open()
            if self._client is not None:
                return self._client
            try:
                nats = importlib.import_module("nats")
            except ImportError as exc:
                raise ConfigurationError(
                    "NATS streams backend requires an optional dependency. Install wybra[streams]."
                ) from exc
            try:
                self._client = await nats.connect(
                    servers=list(self.servers),
                    **self.connection_options,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "NATS streams connection failed (%s).",
                    type(exc).__name__,
                )
                raise StreamsProviderError("NATS streams connection failed.") from None
            return self._client

    def _subject(self, owner: str, topic: str) -> str:
        digest = stream_resource_digest(owner, topic)
        return f"wybra.streams.{self.namespace}.{digest}"

    def _require_open(self) -> None:
        if self._closed:
            raise StreamsProviderError("The NATS streams provider is closed.")


async def _next_message(handle: Any, timeout: float | None) -> Any:
    return await handle.next_msg(timeout=timeout)


async def _unsubscribe(handle: Any) -> None:
    try:
        await handle.unsubscribe()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "NATS streams subscription cleanup failed (%s).",
            type(exc).__name__,
        )


def _is_timeout_error(error: Exception) -> bool:
    try:
        errors = importlib.import_module("nats.errors")
    except ImportError:
        return False
    timeout_error = getattr(errors, "TimeoutError", None)
    return isinstance(timeout_error, type) and isinstance(error, timeout_error)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - asyncio.get_running_loop().time())


__all__ = ("NatsStreamSubscription", "NatsStreamsProvider")
