from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, overload, runtime_checkable

from fastapi import Request

from wybra.messages.records import (
    ERROR_ALERT,
    SUCCESS_ALERT,
    WARNING_ALERT,
    AlertRecord,
)
from wybra.messages.settings import MessagesSettings
from wybra.messages.storage import (
    REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE,
    REQUEST_ALERTS_RENDERED_ATTRIBUTE,
    REQUEST_PEEKED_ALERTS_ATTRIBUTE,
    MessagesStorage,
)


@runtime_checkable
class MessagesCapability(Protocol):
    async def add_alert(
        self,
        request: Request,
        severity: str,
        message: object,
    ) -> None: ...

    async def success(self, request: Request, message: object) -> None: ...

    async def warning(self, request: Request, message: object) -> None: ...

    async def error(self, request: Request, message: object) -> None: ...

    async def peek_alerts(self, request: Request) -> tuple[AlertRecord, ...]: ...

    async def acknowledge_alerts(self, request: Request) -> None: ...

    async def renderable_alerts(self, request: Request) -> RenderableAlerts: ...

    async def consume_alerts(self, request: Request) -> tuple[AlertRecord, ...]: ...

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None: ...

    async def cleanup_expired(self, *, now: float) -> None: ...

    async def validate(self) -> None: ...


RENDERABLE_ALERTS_STATE_ATTRIBUTE = "wybra_messages_renderable_alerts"


@dataclass(frozen=True, slots=True)
class RenderableAlerts(Sequence[AlertRecord]):
    request: Request
    alerts: tuple[AlertRecord, ...]

    def __iter__(self) -> Iterator[AlertRecord]:
        self._mark_rendered()
        return iter(self.alerts)

    def __len__(self) -> int:
        self._mark_rendered()
        return len(self.alerts)

    @overload
    def __getitem__(self, index: int) -> AlertRecord: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[AlertRecord, ...]: ...

    def __getitem__(self, index: int | slice) -> AlertRecord | tuple[AlertRecord, ...]:
        self._mark_rendered()
        return self.alerts[index]

    def __bool__(self) -> bool:
        self._mark_rendered()
        return bool(self.alerts)

    def _mark_rendered(self) -> None:
        setattr(self.request.state, REQUEST_ALERTS_RENDERED_ATTRIBUTE, True)


@dataclass(frozen=True, slots=True)
class DefaultMessagesCapability:
    settings: MessagesSettings
    storage: MessagesStorage

    async def add_alert(
        self,
        request: Request,
        severity: str,
        message: object,
    ) -> None:
        if getattr(request.state, REQUEST_ALERTS_RENDERED_ATTRIBUTE, False):
            await self.storage.acknowledge(request, now=time.time())
        await self.storage.enqueue(
            request,
            AlertRecord.create(
                severity,
                message,
                max_message_length=self.settings.resolved_message_max_length,
            ),
        )
        for attribute in (
            REQUEST_PEEKED_ALERTS_ATTRIBUTE,
            REQUEST_ALERTS_RENDERED_ATTRIBUTE,
            REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE,
            RENDERABLE_ALERTS_STATE_ATTRIBUTE,
        ):
            if hasattr(request.state, attribute):
                delattr(request.state, attribute)

    async def success(self, request: Request, message: object) -> None:
        await self.add_alert(request, SUCCESS_ALERT, message)

    async def warning(self, request: Request, message: object) -> None:
        await self.add_alert(request, WARNING_ALERT, message)

    async def error(self, request: Request, message: object) -> None:
        await self.add_alert(request, ERROR_ALERT, message)

    async def peek_alerts(self, request: Request) -> tuple[AlertRecord, ...]:
        cached = getattr(request.state, REQUEST_PEEKED_ALERTS_ATTRIBUTE, None)
        if cached is not None:
            return cached
        alerts = await self.storage.peek(request, now=time.time())
        setattr(request.state, REQUEST_PEEKED_ALERTS_ATTRIBUTE, alerts)
        return alerts

    async def renderable_alerts(self, request: Request) -> RenderableAlerts:
        cached = getattr(request.state, RENDERABLE_ALERTS_STATE_ATTRIBUTE, None)
        if isinstance(cached, RenderableAlerts):
            return cached
        alerts = RenderableAlerts(
            request=request,
            alerts=await self.peek_alerts(request),
        )
        setattr(request.state, RENDERABLE_ALERTS_STATE_ATTRIBUTE, alerts)
        return alerts

    async def acknowledge_alerts(self, request: Request) -> None:
        await self.storage.acknowledge(request, now=time.time())
        for attribute in (
            REQUEST_PEEKED_ALERTS_ATTRIBUTE,
            REQUEST_ALERTS_RENDERED_ATTRIBUTE,
            REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE,
            RENDERABLE_ALERTS_STATE_ATTRIBUTE,
        ):
            if hasattr(request.state, attribute):
                delattr(request.state, attribute)

    async def consume_alerts(self, request: Request) -> tuple[AlertRecord, ...]:
        cached = getattr(request.state, REQUEST_PEEKED_ALERTS_ATTRIBUTE, None)
        if cached is not None:
            if not getattr(request.state, REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE, False):
                await self.storage.acknowledge(request, now=time.time())
                setattr(request.state, REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE, True)
            return cached
        alerts = await self.storage.pop(request, now=time.time())
        setattr(request.state, REQUEST_PEEKED_ALERTS_ATTRIBUTE, alerts)
        setattr(request.state, REQUEST_ALERTS_ACKNOWLEDGED_ATTRIBUTE, True)
        return alerts

    async def cleanup_session_data(self, session_data: Mapping[str, Any]) -> None:
        await self.storage.cleanup_session_data(session_data)

    async def cleanup_expired(self, *, now: float) -> None:
        await self.storage.cleanup(now=now)

    async def validate(self) -> None:
        await self.storage.validate()

    async def close(self) -> None:
        close = getattr(self.storage, "close", None)
        if close is not None:
            await close()


__all__ = (
    "DefaultMessagesCapability",
    "MessagesCapability",
    "RenderableAlerts",
)
