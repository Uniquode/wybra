from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from fastapi import Request
from sqlalchemy import delete, or_, select

from wybra.db import DatabaseCapability
from wybra.messages.config import MessageStorageBackend
from wybra.messages.exceptions import (
    InvalidAlertError,
    MessageQueueUnavailableError,
    MessagesConfigurationError,
    MessageStorageError,
)
from wybra.messages.models import MessageAlert
from wybra.messages.records import AlertPayload, AlertRecord
from wybra.messages.settings import MessagesSettings
from wybra.site import Site, SiteCapabilityProxy

SESSION_ALERTS_KEY = "_wybra_messages_alerts"
SESSION_QUEUE_ID_KEY = "_wybra_messages_queue_id"
REQUEST_PEEKED_ALERTS_ATTRIBUTE = "wybra_messages_peeked_alerts"
REQUEST_ALERTS_RENDERED_ATTRIBUTE = "wybra_messages_alerts_rendered"
REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE = "wybra_messages_alerts_acknowledged"


class MessagesStorage(Protocol):
    async def enqueue(self, request: Request, alert: AlertRecord) -> None: ...

    async def peek(
        self, request: Request, *, now: float
    ) -> tuple[AlertRecord, ...]: ...

    async def acknowledge(self, request: Request, *, now: float) -> None: ...

    async def pop(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]: ...

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None: ...

    async def cleanup(self, *, now: float) -> None: ...

    async def validate(self) -> None: ...


class CacheQueueBackend(Protocol):
    async def append(
        self,
        queue_key: str,
        payload: AlertPayload,
        *,
        queue_depth: int,
        ttl_seconds: float,
    ) -> None: ...

    async def peek(self, queue_key: str) -> tuple[AlertPayload, ...]: ...

    async def acknowledge(self, queue_key: str) -> None: ...

    async def pop(self, queue_key: str) -> tuple[AlertPayload, ...]: ...

    async def validate(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionMessagesStorage:
    settings: MessagesSettings

    async def enqueue(self, request: Request, alert: AlertRecord) -> None:
        session = request_session(request)
        queue = _valid_payloads(
            session.get(SESSION_ALERTS_KEY, ()),
            max_message_length=self.settings.resolved_message_max_length,
            now=None,
        )
        queue.append(_stored_payload(alert, self._expires_at(alert.created_at)))
        session[SESSION_ALERTS_KEY] = queue[-self.settings.resolved_queue_depth :]

    async def peek(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        session = request_session(request)
        raw_queue = session.get(SESSION_ALERTS_KEY, ())
        payloads = _valid_payloads(
            raw_queue,
            max_message_length=self.settings.resolved_message_max_length,
            now=now,
        )
        return _records_from_payloads(
            payloads,
            max_message_length=self.settings.resolved_message_max_length,
        )

    async def acknowledge(self, request: Request, *, now: float) -> None:
        request_session(request).pop(SESSION_ALERTS_KEY, None)

    async def pop(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        alerts = await self.peek(request, now=now)
        await self.acknowledge(request, now=now)
        return alerts

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        return None

    async def cleanup(self, *, now: float) -> None:
        return None

    async def validate(self) -> None:
        return None

    def _expires_at(self, created_at: float) -> float:
        return created_at + self.settings.resolved_message_ttl_seconds


@dataclass(frozen=True, slots=True)
class CacheMessagesStorage:
    settings: MessagesSettings
    backend: CacheQueueBackend

    async def enqueue(self, request: Request, alert: AlertRecord) -> None:
        payload = _stored_payload(
            alert,
            alert.created_at + self.settings.resolved_message_ttl_seconds,
        )
        await self.backend.append(
            server_side_queue_key(
                request,
                prefix=self.settings.cache_key_prefix,
            ),
            payload,
            queue_depth=self.settings.resolved_queue_depth,
            ttl_seconds=self.settings.resolved_message_ttl_seconds,
        )

    async def peek(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        queue_key = optional_server_side_queue_key(
            request,
            prefix=self.settings.cache_key_prefix,
        )
        if queue_key is None:
            return ()
        payloads = _valid_payloads(
            await self.backend.peek(queue_key),
            max_message_length=self.settings.resolved_message_max_length,
            now=now,
        )
        return _records_from_payloads(
            payloads,
            max_message_length=self.settings.resolved_message_max_length,
        )

    async def acknowledge(self, request: Request, *, now: float) -> None:
        queue_key = optional_server_side_queue_key(
            request,
            prefix=self.settings.cache_key_prefix,
        )
        if queue_key is not None:
            await self.backend.acknowledge(queue_key)

    async def pop(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        alerts = await self.peek(request, now=now)
        await self.acknowledge(request, now=now)
        return alerts

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        queue_key = server_side_queue_key_from_session_data(
            session_data,
            prefix=self.settings.cache_key_prefix,
        )
        if queue_key is not None:
            await self.backend.acknowledge(queue_key)

    async def cleanup(self, *, now: float) -> None:
        return None

    async def validate(self) -> None:
        await self.backend.validate()

    async def close(self) -> None:
        await self.backend.close()


@dataclass(slots=True)
class InMemoryCacheQueueBackend:
    _queues: dict[str, list[AlertPayload]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def append(
        self,
        queue_key: str,
        payload: AlertPayload,
        *,
        queue_depth: int,
        ttl_seconds: float,
    ) -> None:
        async with self._lock:
            queue = list(self._queues.get(queue_key, ()))
            queue.append(payload)
            self._queues[queue_key] = queue[-queue_depth:]

    async def pop(self, queue_key: str) -> tuple[AlertPayload, ...]:
        async with self._lock:
            return tuple(self._queues.pop(queue_key, ()))

    async def peek(self, queue_key: str) -> tuple[AlertPayload, ...]:
        async with self._lock:
            return tuple(self._queues.get(queue_key, ()))

    async def acknowledge(self, queue_key: str) -> None:
        async with self._lock:
            self._queues.pop(queue_key, None)

    async def validate(self) -> None:
        return None

    async def close(self) -> None:
        async with self._lock:
            self._queues.clear()


@dataclass(slots=True)
class RedisCacheQueueBackend:
    url: str
    _client: Any = field(default=None, init=False, repr=False)

    async def append(
        self,
        queue_key: str,
        payload: AlertPayload,
        *,
        queue_depth: int,
        ttl_seconds: float,
    ) -> None:
        client = self._redis_client()
        raw_queue = await client.get(queue_key)
        queue = _payloads_from_json(raw_queue)
        queue.append(payload)
        queue = queue[-queue_depth:]
        await client.set(queue_key, json.dumps(queue), ex=max(1, int(ttl_seconds)))

    async def pop(self, queue_key: str) -> tuple[AlertPayload, ...]:
        client = self._redis_client()
        if hasattr(client, "getdel"):
            raw_queue = await client.getdel(queue_key)
        else:
            raw_queue = await client.get(queue_key)
            if raw_queue is not None:
                await client.delete(queue_key)
        return tuple(_payloads_from_json(raw_queue))

    async def peek(self, queue_key: str) -> tuple[AlertPayload, ...]:
        return tuple(_payloads_from_json(await self._redis_client().get(queue_key)))

    async def acknowledge(self, queue_key: str) -> None:
        await self._redis_client().delete(queue_key)

    async def validate(self) -> None:
        client = self._redis_client()
        try:
            await client.ping()
        except Exception as exc:  # pragma: no cover - depends on external service
            raise MessageStorageError("Redis messages cache is unavailable.") from exc

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        self._client = None

    def _redis_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            redis_module = importlib.import_module("redis.asyncio")
        except ImportError as exc:
            raise MessagesConfigurationError(
                "Redis messages cache requires the optional redis package."
            ) from exc
        self._client = redis_module.Redis.from_url(self.url, decode_responses=True)
        return self._client


@dataclass(frozen=True, slots=True)
class DatabaseMessagesStorage:
    settings: MessagesSettings
    database: SiteCapabilityProxy[DatabaseCapability]

    async def enqueue(self, request: Request, alert: AlertRecord) -> None:
        queue_key = server_side_queue_key(request, prefix="")
        async with self.database.require().transaction(
            self.settings.database_connection_name
        ) as session:
            session.add(
                MessageAlert(
                    queue_key=queue_key,
                    severity=alert.severity,
                    message=alert.message,
                    created_at=alert.created_at,
                    expires_at=(
                        alert.created_at + self.settings.resolved_message_ttl_seconds
                    ),
                )
            )
            await session.flush()
            await self._trim_queue(session, queue_key)

    async def peek(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        queue_key = optional_server_side_queue_key(request, prefix="")
        if queue_key is None:
            return ()
        async with self.database.require().transaction(
            self.settings.database_connection_name
        ) as session:
            result = await session.execute(
                select(MessageAlert)
                .where(MessageAlert.queue_key == queue_key)
                .where(
                    or_(
                        MessageAlert.expires_at.is_(None),
                        MessageAlert.expires_at > now,
                    )
                )
                .order_by(MessageAlert.id)
            )
            rows = tuple(result.scalars())
            if not rows:
                return ()
            return tuple(
                AlertRecord.create(
                    row.severity,
                    row.message,
                    created_at=row.created_at,
                    max_message_length=self.settings.resolved_message_max_length,
                )
                for row in rows
            )

    async def acknowledge(self, request: Request, *, now: float) -> None:
        queue_key = optional_server_side_queue_key(request, prefix="")
        if queue_key is None:
            return
        async with self.database.require().transaction(
            self.settings.database_connection_name
        ) as session:
            await session.execute(
                delete(MessageAlert).where(MessageAlert.queue_key == queue_key)
            )

    async def pop(self, request: Request, *, now: float) -> tuple[AlertRecord, ...]:
        alerts = await self.peek(request, now=now)
        await self.acknowledge(request, now=now)
        return alerts

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        queue_key = server_side_queue_key_from_session_data(session_data, prefix="")
        if queue_key is None:
            return
        async with self.database.require().transaction(
            self.settings.database_connection_name
        ) as session:
            await self._delete_queue(session, queue_key)

    async def cleanup(self, *, now: float) -> None:
        async with self.database.require().transaction(
            self.settings.database_connection_name
        ) as session:
            await self._delete_expired(session, now)

    async def validate(self) -> None:
        self.database.require()

    async def _delete_expired(self, session: Any, now: float) -> None:
        await session.execute(
            delete(MessageAlert)
            .where(MessageAlert.expires_at.is_not(None))
            .where(MessageAlert.expires_at <= now)
        )

    async def _delete_queue(self, session: Any, queue_key: str) -> None:
        await session.execute(
            delete(MessageAlert).where(MessageAlert.queue_key == queue_key)
        )

    async def _trim_queue(self, session: Any, queue_key: str) -> None:
        result = await session.execute(
            select(MessageAlert.id)
            .where(MessageAlert.queue_key == queue_key)
            .order_by(MessageAlert.id.desc())
            .offset(self.settings.resolved_queue_depth)
        )
        stale_ids = tuple(result.scalars())
        if stale_ids:
            await session.execute(
                delete(MessageAlert).where(MessageAlert.id.in_(stale_ids))
            )


def storage_from_settings(site: Site, settings: MessagesSettings) -> MessagesStorage:
    if settings.resolved_storage_backend is MessageStorageBackend.SESSION:
        return SessionMessagesStorage(settings)
    if settings.resolved_storage_backend is MessageStorageBackend.CACHE:
        assert settings.cache_url is not None
        return CacheMessagesStorage(
            settings=settings,
            backend=cache_backend_from_url(settings.cache_url),
        )
    if settings.resolved_storage_backend is MessageStorageBackend.DATABASE:
        return DatabaseMessagesStorage(
            settings=settings,
            database=site.capability_proxy(DatabaseCapability),
        )
    raise MessagesConfigurationError("Unsupported messages storage backend.")


def cache_backend_from_url(url: str) -> CacheQueueBackend:
    parsed = urlparse(url)
    if parsed.scheme == "memory":
        return InMemoryCacheQueueBackend()
    if parsed.scheme in {"redis", "rediss"}:
        return RedisCacheQueueBackend(url)
    raise MessagesConfigurationError(
        "wybra.messages.cache_url must use memory://, redis://, or rediss://."
    )


def request_session(request: Request) -> MutableMapping[str, Any]:
    session = request.scope.get("session")
    if session is None:
        raise MessageQueueUnavailableError(
            "Messages session storage requires Wybra sessions middleware to provide "
            "a compatible request.session mapping."
        )
    if not isinstance(session, MutableMapping):
        raise MessageQueueUnavailableError(
            "Messages storage requires request.session to be a mutable mapping."
        )
    return session


def server_side_queue_key(request: Request, *, prefix: str) -> str:
    session = request_session(request)
    value = session.get(SESSION_QUEUE_ID_KEY)
    if isinstance(value, str) and value.strip():
        queue_id = value.strip()
    else:
        queue_id = uuid.uuid4().hex
        session[SESSION_QUEUE_ID_KEY] = queue_id
    return f"{prefix}{queue_id}"


def optional_server_side_queue_key(request: Request, *, prefix: str) -> str | None:
    session = request_session(request)
    return server_side_queue_key_from_session_data(session, prefix=prefix)


def server_side_queue_key_from_session_data(
    session_data: Mapping[str, Any],
    *,
    prefix: str,
) -> str | None:
    value = session_data.get(SESSION_QUEUE_ID_KEY)
    if isinstance(value, str) and value.strip():
        return f"{prefix}{value.strip()}"
    return None


def _stored_payload(alert: AlertRecord, expires_at: float) -> AlertPayload:
    payload = alert.to_payload()
    payload["expires_at"] = expires_at
    return payload


def _valid_payloads(
    raw_queue: object,
    *,
    max_message_length: int,
    now: float | None,
) -> list[AlertPayload]:
    if not isinstance(raw_queue, Sequence) or isinstance(raw_queue, (str, bytes)):
        return []
    payloads: list[AlertPayload] = []
    for raw_payload in raw_queue:
        if not isinstance(raw_payload, Mapping):
            continue
        payload = dict(raw_payload)
        expires_at = payload.get("expires_at")
        if (
            isinstance(expires_at, (int, float))
            and now is not None
            and expires_at <= now
        ):
            continue
        try:
            AlertRecord.from_payload(payload, max_message_length=max_message_length)
        except InvalidAlertError:
            continue
        payloads.append(payload)
    return payloads


def _records_from_payloads(
    payloads: Sequence[Mapping[str, object]],
    *,
    max_message_length: int,
) -> tuple[AlertRecord, ...]:
    return tuple(
        AlertRecord.from_payload(payload, max_message_length=max_message_length)
        for payload in payloads
    )


def _payloads_from_json(value: object) -> list[AlertPayload]:
    if value is None:
        return []
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [cast(AlertPayload, item) for item in decoded if isinstance(item, dict)]


__all__ = (
    "REQUEST_ALERTS_RENDERED_ATTRIBUTE",
    "REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE",
    "REQUEST_PEEKED_ALERTS_ATTRIBUTE",
    "SESSION_ALERTS_KEY",
    "SESSION_QUEUE_ID_KEY",
    "CacheMessagesStorage",
    "CacheQueueBackend",
    "DatabaseMessagesStorage",
    "InMemoryCacheQueueBackend",
    "MessagesStorage",
    "RedisCacheQueueBackend",
    "SessionMessagesStorage",
    "cache_backend_from_url",
    "request_session",
    "server_side_queue_key",
    "storage_from_settings",
)
