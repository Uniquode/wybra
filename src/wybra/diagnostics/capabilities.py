"""Transport-neutral bounded diagnostics snapshots and subscriptions."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from uuid import uuid7

from wybra.diagnostics.events import DiagnosticLevel, RequestDiagnostics
from wybra.events import EventScope

_DEFAULT_SUBSCRIPTION_QUEUE_LIMIT: Final = 32
_DEFAULT_SNAPSHOT_EVENT_LIMIT: Final = 1_000
_PROCESS_CAPABILITY: DiagnosticsCapability | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    """An immutable, secret-safe completed diagnostics context."""

    identifier: str
    summary: dict[str, object]
    events: tuple[dict[str, object], ...]
    truncated: bool = False

    @classmethod
    def from_request(
        cls,
        diagnostics: RequestDiagnostics,
        *,
        event_limit: int,
    ) -> DiagnosticSnapshot:
        events = tuple(event.as_dict() for event in diagnostics.events)
        return cls(
            identifier=str(uuid7()),
            summary=diagnostics.summary(),
            events=events[:event_limit],
            truncated=len(events) > event_limit,
        )

    def for_scope(self, scope: EventScope) -> DiagnosticSnapshot:
        events = tuple(
            event for event in self.events if _event_matches_scope(event, scope)
        )
        return DiagnosticSnapshot(
            identifier=self.identifier,
            summary=dict(self.summary),
            events=events,
            truncated=self.truncated,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "summary": dict(self.summary),
            "events": [dict(event) for event in self.events],
            "truncated": self.truncated,
        }


class DiagnosticsSubscription:
    """One consumer's bounded asynchronous diagnostic notification queue."""

    def __init__(self, scopes: Iterable[EventScope], *, queue_limit: int) -> None:
        self._scopes = tuple(scopes)
        self._queue: asyncio.Queue[DiagnosticSnapshot] = asyncio.Queue(queue_limit)
        self._closed = False
        self._dropped = False

    @property
    def scopes(self) -> tuple[EventScope, ...]:
        return self._scopes

    @property
    def dropped(self) -> bool:
        return self._dropped

    async def receive(self) -> DiagnosticSnapshot:
        """Wait for the next matching completed snapshot."""

        return await self._queue.get()

    def close(self) -> None:
        """Prevent further notifications to this subscription."""

        self._closed = True

    def take_dropped(self) -> bool:
        """Return and clear the notification-overflow indicator."""

        dropped = self._dropped
        self._dropped = False
        return dropped

    def _publish(self, snapshot: DiagnosticSnapshot) -> None:
        if self._closed:
            return
        scoped = _snapshot_for_scopes(snapshot, self._scopes)
        if scoped is None:
            return
        if self._queue.full():
            self._dropped = True
            return
        self._queue.put_nowait(scoped)


class DiagnosticsCapability:
    """Owns process-local retained diagnostics and in-process subscriptions."""

    def __init__(
        self,
        *,
        retention_limit: int = 100,
        subscription_queue_limit: int = _DEFAULT_SUBSCRIPTION_QUEUE_LIMIT,
        snapshot_event_limit: int = _DEFAULT_SNAPSHOT_EVENT_LIMIT,
        allowed_scopes: Iterable[EventScope] | None = None,
        level: DiagnosticLevel = "info",
    ) -> None:
        if retention_limit <= 0:
            raise ValueError("Diagnostics retention_limit must be positive.")
        if subscription_queue_limit <= 0:
            raise ValueError("Diagnostics subscription_queue_limit must be positive.")
        if snapshot_event_limit <= 0:
            raise ValueError("Diagnostics snapshot_event_limit must be positive.")
        self._snapshots: deque[DiagnosticSnapshot] = deque(maxlen=retention_limit)
        self._subscriptions: set[DiagnosticsSubscription] = set()
        self._subscriptions_by_scope: dict[
            EventScope,
            set[DiagnosticsSubscription],
        ] = {}
        self._subscription_queue_limit = subscription_queue_limit
        self._snapshot_event_limit = snapshot_event_limit
        self._allowed_scopes = (
            tuple(allowed_scopes) if allowed_scopes is not None else None
        )
        self.level = level

    def snapshots(
        self,
        scope: EventScope,
        *,
        include_empty: bool = False,
    ) -> tuple[DiagnosticSnapshot, ...]:
        """Return retained snapshots containing records selected by ``scope``."""

        return tuple(
            scoped
            for snapshot in self._snapshots
            for scoped in (snapshot.for_scope(scope),)
            if include_empty or scoped.events
        )

    async def subscribe(
        self,
        scopes: Iterable[EventScope],
    ) -> DiagnosticsSubscription:
        """Create an explicitly scoped subscription with a bounded queue."""

        selected_scopes = tuple(scopes)
        if not selected_scopes:
            raise ValueError(
                "Diagnostics subscriptions must select at least one scope."
            )
        if self._allowed_scopes is not None and any(
            not _scope_is_permitted(scope, self._allowed_scopes)
            for scope in selected_scopes
        ):
            raise ValueError("Diagnostics subscription expands the collector filter.")
        subscription = DiagnosticsSubscription(
            selected_scopes,
            queue_limit=self._subscription_queue_limit,
        )
        self._subscriptions.add(subscription)
        for scope in subscription.scopes:
            self._subscriptions_by_scope.setdefault(scope, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: DiagnosticsSubscription) -> None:
        """Remove and close a subscription owned by an in-process consumer."""

        subscription.close()
        self._subscriptions.discard(subscription)
        for scope in subscription.scopes:
            subscriptions = self._subscriptions_by_scope.get(scope)
            if subscriptions is None:
                continue
            subscriptions.discard(subscription)
            if not subscriptions:
                del self._subscriptions_by_scope[scope]

    def replace_collector_scopes(self, scopes: Iterable[EventScope]) -> None:
        """Atomically replace the process-local collector selector."""

        replacement = tuple(scopes)
        if not replacement:
            raise ValueError("Diagnostics collector scopes must not be empty.")
        self._allowed_scopes = replacement

    def selects_diagnostics(self, diagnostics: RequestDiagnostics) -> bool:
        """Return whether a context-free observation matches this collector."""

        if self._allowed_scopes is None:
            return bool(diagnostics.events)
        return any(
            any(
                _event_matches_scope(event.as_dict(), scope)
                for scope in self._allowed_scopes
            )
            for event in diagnostics.events
        )

    async def close(self) -> None:
        """Release process-wide registration and local subscribers."""

        deactivate_process_diagnostics(self)
        for subscription in tuple(self._subscriptions):
            self.unsubscribe(subscription)

    def record_completed(self, diagnostics: RequestDiagnostics) -> None:
        """Retain and fan out a finalised request diagnostics context."""

        snapshot = DiagnosticSnapshot.from_request(
            diagnostics,
            event_limit=self._snapshot_event_limit,
        )
        if self._allowed_scopes is not None:
            snapshot = _snapshot_for_scopes(snapshot, self._allowed_scopes)
        self._snapshots.append(snapshot)
        for subscription in self._matching_subscriptions(snapshot):
            subscription._publish(snapshot)

    def _matching_subscriptions(
        self,
        snapshot: DiagnosticSnapshot,
    ) -> tuple[DiagnosticsSubscription, ...]:
        return tuple(
            {
                subscription
                for scope, subscriptions in self._subscriptions_by_scope.items()
                if snapshot.for_scope(scope).events
                for subscription in subscriptions
            }
        )


def _event_matches_scope(event: dict[str, object], scope: EventScope) -> bool:
    category = event.get("category")
    name = event.get("name")
    if not isinstance(category, str):
        return False
    event_scope = EventScope(
        (category, *name.split(".")) if isinstance(name, str) and name else (category,)
    )
    return event_scope.matches(scope)


def _snapshot_for_scopes(
    snapshot: DiagnosticSnapshot,
    scopes: Iterable[EventScope],
) -> DiagnosticSnapshot:
    selected = tuple(
        event
        for event in snapshot.events
        if any(_event_matches_scope(event, scope) for scope in scopes)
    )
    return DiagnosticSnapshot(
        identifier=snapshot.identifier,
        summary=dict(snapshot.summary),
        events=selected,
        truncated=snapshot.truncated,
    )


def _scope_is_permitted(
    scope: EventScope,
    allowed_scopes: Iterable[EventScope],
) -> bool:
    return any(scope.matches(allowed) for allowed in allowed_scopes)


def activate_process_diagnostics(capability: DiagnosticsCapability) -> None:
    """Make one enabled capability receive context-free process observations."""

    global _PROCESS_CAPABILITY
    _PROCESS_CAPABILITY = capability


def process_diagnostics_capability() -> DiagnosticsCapability | None:
    """Return the active process-wide diagnostics collector, when enabled."""

    return _PROCESS_CAPABILITY


def deactivate_process_diagnostics(capability: DiagnosticsCapability) -> None:
    """Remove ``capability`` only when it remains the active collector."""

    global _PROCESS_CAPABILITY
    if _PROCESS_CAPABILITY is capability:
        _PROCESS_CAPABILITY = None


__all__ = (
    "DiagnosticSnapshot",
    "DiagnosticsCapability",
    "DiagnosticsSubscription",
    "activate_process_diagnostics",
    "deactivate_process_diagnostics",
    "process_diagnostics_capability",
)
