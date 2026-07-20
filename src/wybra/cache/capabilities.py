from __future__ import annotations

import asyncio
import hashlib
import importlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from wybra.core.exceptions import ConfigurationError
from wybra.events import (
    CACHE_DELETE,
    CACHE_FILL,
    CACHE_READ,
    CACHE_SET,
    EVT_CACHE,
    CacheOperationCompletedEvent,
    CacheOperationFailedEvent,
    EventsCapability,
    EventSegment,
    event_delivery_enabled,
    publish_observation,
    scoped,
)

type CacheFactory = Callable[[], Awaitable[bytes]]
DEFAULT_CACHE_FILL_TIMEOUT_SECONDS = 30.0


@runtime_checkable
class CacheCapability(Protocol):
    async def get(self, owner: str, key: str) -> bytes | None: ...

    async def set(self, owner: str, key: str, value: bytes, *, ttl: float) -> None: ...

    async def delete(self, owner: str, key: str) -> None: ...

    async def get_or_set(
        self,
        owner: str,
        key: str,
        *,
        ttl: float,
        factory: CacheFactory,
        timeout: float = DEFAULT_CACHE_FILL_TIMEOUT_SECONDS,
    ) -> bytes: ...


@dataclass(slots=True)
class _SingleFlightCache:
    """Coordinate one in-process cache fill for each backend key."""

    events: EventsCapability | None = field(default=None, kw_only=True)
    _fills: dict[str, asyncio.Event] = field(default_factory=dict, init=False)
    _fills_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def get(self, owner: str, key: str) -> bytes | None:
        raise NotImplementedError

    async def set(self, owner: str, key: str, value: bytes, *, ttl: float) -> None:
        raise NotImplementedError

    async def _get_or_set(
        self,
        owner: str,
        key: str,
        *,
        ttl: float,
        factory: CacheFactory,
        timeout: float,
    ) -> bytes:
        cache_key = _cache_key(owner, key)
        timeout = _fill_timeout(timeout)
        while True:
            value = await self.get(owner, key)
            if value is not None:
                return value

            async with self._fills_lock:
                completed = self._fills.get(cache_key)
                if completed is None:
                    completed = asyncio.Event()
                    self._fills[cache_key] = completed
                    is_filler = True
                else:
                    is_filler = False

            if not is_filler:
                await asyncio.wait_for(completed.wait(), timeout=timeout)
                continue

            fill_started = time.perf_counter()
            try:
                try:
                    value = await asyncio.wait_for(factory(), timeout=timeout)
                    await self.set(owner, key, value, ttl=ttl)
                finally:
                    await self._release_fill(cache_key, completed)
            except Exception as exc:
                await self._record_failed(
                    CACHE_FILL,
                    owner,
                    key,
                    started=fill_started,
                    error=exc,
                )
                raise
            else:
                await self._record_completed(
                    CACHE_FILL,
                    owner,
                    key,
                    outcome="filled",
                    started=fill_started,
                )
                return value

    async def _release_fill(self, cache_key: str, completed: asyncio.Event) -> None:
        """Wake waiters before observational event delivery can delay them."""

        async with self._fills_lock:
            if self._fills.get(cache_key) is completed:
                self._fills.pop(cache_key, None)
            completed.set()

    async def _record_completed(
        self,
        operation: EventSegment,
        owner: str,
        key: str,
        *,
        outcome: str,
        started: float,
    ) -> None:
        events = self.events
        if events is None or not event_delivery_enabled(events):
            return
        with scoped(EVT_CACHE(operation)):
            await publish_observation(
                events,
                CacheOperationCompletedEvent(
                    owner=owner,
                    key_fingerprint=_key_fingerprint(owner, key),
                    outcome=outcome,
                    duration_seconds=time.perf_counter() - started,
                ),
                message="cache operation event",
            )

    async def _record_failed(
        self,
        operation: EventSegment,
        owner: str,
        key: str,
        *,
        started: float,
        error: Exception,
    ) -> None:
        events = self.events
        if events is None or not event_delivery_enabled(events):
            return
        with scoped(EVT_CACHE(operation)):
            await publish_observation(
                events,
                CacheOperationFailedEvent(
                    owner=owner,
                    key_fingerprint=_key_fingerprint(owner, key),
                    operation=str(operation),
                    duration_seconds=time.perf_counter() - started,
                    error_type=type(error).__name__,
                ),
                message="cache operation failure event",
            )


@dataclass(slots=True)
class InMemoryCache(_SingleFlightCache):
    _entries: dict[str, tuple[float, bytes]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self, owner: str, key: str) -> bytes | None:
        started = time.perf_counter()
        cache_key = _cache_key(owner, key)
        try:
            async with self._lock:
                entry = self._entries.get(cache_key)
                if entry is None:
                    value = None
                    outcome = "miss"
                else:
                    expires_at, value = entry
                    if expires_at <= time.monotonic():
                        self._entries.pop(cache_key, None)
                        value = None
                        outcome = "expired"
                    else:
                        outcome = "hit"
        except Exception as exc:
            await self._record_failed(
                CACHE_READ, owner, key, started=started, error=exc
            )
            raise
        await self._record_completed(
            CACHE_READ, owner, key, outcome=outcome, started=started
        )
        return value

    async def set(self, owner: str, key: str, value: bytes, *, ttl: float) -> None:
        started = time.perf_counter()
        if not isinstance(value, bytes):
            raise TypeError("Cache values must be bytes.")
        cache_key = _cache_key(owner, key)
        expires_at = time.monotonic() + _ttl(ttl)
        try:
            async with self._lock:
                self._entries[cache_key] = (expires_at, value)
        except Exception as exc:
            await self._record_failed(CACHE_SET, owner, key, started=started, error=exc)
            raise
        await self._record_completed(
            CACHE_SET, owner, key, outcome="stored", started=started
        )

    async def delete(self, owner: str, key: str) -> None:
        started = time.perf_counter()
        try:
            async with self._lock:
                self._entries.pop(_cache_key(owner, key), None)
        except Exception as exc:
            await self._record_failed(
                CACHE_DELETE, owner, key, started=started, error=exc
            )
            raise
        await self._record_completed(
            CACHE_DELETE, owner, key, outcome="deleted", started=started
        )

    async def get_or_set(
        self,
        owner: str,
        key: str,
        *,
        ttl: float,
        factory: CacheFactory,
        timeout: float = DEFAULT_CACHE_FILL_TIMEOUT_SECONDS,
    ) -> bytes:
        return await self._get_or_set(
            owner,
            key,
            ttl=ttl,
            factory=factory,
            timeout=timeout,
        )


@dataclass(slots=True)
class RedisCache(_SingleFlightCache):
    url: str
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._redis_client()

    async def get(self, owner: str, key: str) -> bytes | None:
        started = time.perf_counter()
        try:
            value = await self._redis_client().get(_cache_key(owner, key))
        except Exception as exc:
            await self._record_failed(
                CACHE_READ, owner, key, started=started, error=exc
            )
            raise
        result = value if isinstance(value, bytes) else None
        await self._record_completed(
            CACHE_READ,
            owner,
            key,
            outcome="hit" if result is not None else "miss",
            started=started,
        )
        return result

    async def set(self, owner: str, key: str, value: bytes, *, ttl: float) -> None:
        started = time.perf_counter()
        if not isinstance(value, bytes):
            raise TypeError("Cache values must be bytes.")
        try:
            await self._redis_client().set(
                _cache_key(owner, key),
                value,
                px=max(1, round(_ttl(ttl) * 1000)),
            )
        except Exception as exc:
            await self._record_failed(CACHE_SET, owner, key, started=started, error=exc)
            raise
        await self._record_completed(
            CACHE_SET, owner, key, outcome="stored", started=started
        )

    async def delete(self, owner: str, key: str) -> None:
        started = time.perf_counter()
        try:
            await self._redis_client().delete(_cache_key(owner, key))
        except Exception as exc:
            await self._record_failed(
                CACHE_DELETE, owner, key, started=started, error=exc
            )
            raise
        await self._record_completed(
            CACHE_DELETE, owner, key, outcome="deleted", started=started
        )

    async def get_or_set(
        self,
        owner: str,
        key: str,
        *,
        ttl: float,
        factory: CacheFactory,
        timeout: float = DEFAULT_CACHE_FILL_TIMEOUT_SECONDS,
    ) -> bytes:
        return await self._get_or_set(
            owner,
            key,
            ttl=ttl,
            factory=factory,
            timeout=timeout,
        )

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        await client.aclose()
        self._client = None

    def _redis_client(self) -> Any:
        if self._client is None:
            try:
                redis_module = importlib.import_module("redis.asyncio")
            except ImportError as exc:
                raise ConfigurationError(
                    "Redis cache backend requires the optional cache dependency. "
                    "Install wybra[cache]."
                ) from exc
            self._client = redis_module.Redis.from_url(self.url, decode_responses=False)
        return self._client


def _cache_key(owner: str, key: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Cache owner must be a non-blank string.")
    if ":" in owner:
        raise ValueError("Cache owner must not contain ':'.")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Cache key must be a non-blank string.")
    return f"{owner.strip()}:{key}"


def _key_fingerprint(owner: str, key: str) -> str:
    """Return a stable opaque cache-key identifier for observations."""

    return hashlib.sha256(_cache_key(owner, key).encode()).hexdigest()[:16]


def _ttl(value: float) -> float:
    if not isinstance(value, int | float) or value <= 0:
        raise ValueError("Cache TTL must be positive.")
    return float(value)


def _fill_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError("Cache fill timeout must be positive.")
    return float(value)


__all__ = (
    "CacheCapability",
    "CacheFactory",
    "DEFAULT_CACHE_FILL_TIMEOUT_SECONDS",
    "InMemoryCache",
    "RedisCache",
)
