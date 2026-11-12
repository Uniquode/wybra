from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from wybra.streams.contracts import (
    StreamSubscription,
    validate_stream_payload,
    validate_stream_resource,
)
from wybra.streams.settings import StreamsConfigurationDiagnostic


class StreamsProvider(Protocol):
    async def publish(self, owner: str, topic: str, payload: bytes) -> None: ...

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class DefaultStreamsCapability:
    _provider: StreamsProvider
    _diagnostic: StreamsConfigurationDiagnostic

    async def publish(self, owner: str, topic: str, payload: bytes) -> None:
        owner = validate_stream_resource(owner, label="stream owner")
        topic = validate_stream_resource(topic, label="stream topic")
        payload = validate_stream_payload(payload)
        await self._provider.publish(owner, topic, payload)

    async def subscribe(self, owner: str, topic: str) -> StreamSubscription:
        owner = validate_stream_resource(owner, label="stream owner")
        topic = validate_stream_resource(topic, label="stream topic")
        return await self._provider.subscribe(owner, topic)

    def diagnostics(self) -> StreamsConfigurationDiagnostic:
        return replace(self._diagnostic, health="ready")

    async def close(self) -> None:
        await self._provider.close()


__all__ = ("DefaultStreamsCapability",)
