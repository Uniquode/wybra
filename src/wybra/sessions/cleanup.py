from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from wybra.site import Site

SessionDataCleanupHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class SessionCleanupRegistry:
    _handlers: list[SessionDataCleanupHandler] = field(default_factory=list)

    def register(self, handler: SessionDataCleanupHandler) -> None:
        self._handlers.append(handler)

    async def cleanup_session_data(self, data: Mapping[str, Any]) -> None:
        if not data:
            return
        for handler in tuple(self._handlers):
            await handler(data)


def session_cleanup_registry_from_site(site: Site) -> SessionCleanupRegistry:
    registry = site.optional_capability(SessionCleanupRegistry)
    if registry is None:
        registry = SessionCleanupRegistry()
        site.provide_capability(SessionCleanupRegistry, registry)
    return registry


__all__ = (
    "SessionCleanupRegistry",
    "SessionDataCleanupHandler",
    "session_cleanup_registry_from_site",
)
