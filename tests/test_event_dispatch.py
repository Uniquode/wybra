from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import ClassVar, cast

import pytest
from fastapi import FastAPI

from wybra.config import MappingConfigSource
from wybra.diagnostics import (
    DiagnosticsCapability,
    diagnostic_context,
)
from wybra.events import (
    BEGIN,
    EVT_EVENTS,
    EVT_EVENTS_ERRORS,
    EVT_SQL,
    TRANSACTION,
    Event,
    EventDispatcher,
    EventHandler,
    EventOutcome,
    EventsCapability,
    event_segment,
    extend,
    observe_operation,
    scope,
)
from wybra.site import Site, start


@dataclass(frozen=True, slots=True)
class TransactionStarted(Event):
    kind: ClassVar = BEGIN
    transaction_id: str


@dataclass(frozen=True, slots=True)
class TransactionOnly(Event):
    kind: ClassVar = event_segment("transactionsonly")
    transaction_id: str


@dataclass(frozen=True, slots=True)
class CacheInvalidationStarted(Event):
    kind: ClassVar = event_segment("started")
    owner: str
    key: str


@dataclass(frozen=True, slots=True)
class CacheInvalidated(Event):
    kind: ClassVar = event_segment("finished")
    outcome: EventOutcome


class UnavailableEvents:
    """A capability implementation that simulates an unavailable event service."""

    def subscribe(self, _selector: object, _handler: object) -> None:
        pass

    async def publish(self, _event: Event) -> None:
        raise RuntimeError("event service unavailable")


class TestEventDispatcher:
    @pytest.mark.anyio
    async def test_dispatches_matching_handlers_in_registration_order(self) -> None:
        dispatcher = EventDispatcher()
        observed: list[str] = []

        async def root_handler(event: Event) -> None:
            observed.append(f"root:{event.scope}")

        async def transaction_handler(event: Event) -> None:
            observed.append(f"transaction:{event.scope}")

        dispatcher.subscribe(EVT_SQL, root_handler)
        dispatcher.subscribe(EVT_SQL(TRANSACTION), transaction_handler)

        @extend(TRANSACTION)
        async def publish_transaction() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="transaction-1"))

        @scope(EVT_SQL)
        async def publish() -> None:
            await publish_transaction()

        await publish()

        assert observed == [
            "root:sql.transaction.begin",
            "transaction:sql.transaction.begin",
        ]

    @pytest.mark.anyio
    async def test_prefix_subscription_does_not_match_partial_segments(self) -> None:
        dispatcher = EventDispatcher()
        observed: list[str] = []

        async def transaction_handler(event: Event) -> None:
            observed.append(str(event.scope))

        dispatcher.subscribe(EVT_SQL(TRANSACTION), transaction_handler)

        @scope(EVT_SQL)
        async def publish() -> None:
            await dispatcher.publish(TransactionOnly(transaction_id="transaction-1"))

        await publish()

        assert observed == []

    @pytest.mark.anyio
    async def test_handler_failure_is_logged_and_later_handlers_still_run(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dispatcher = EventDispatcher()
        observed: list[str] = []

        async def failing_handler(_event: Event) -> None:
            raise RuntimeError("expected handler failure")

        async def later_handler(event: Event) -> None:
            observed.append(str(event.scope))

        dispatcher.subscribe(EVT_SQL, failing_handler)
        dispatcher.subscribe(EVT_SQL, later_handler)

        @scope(EVT_SQL)
        async def publish() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="transaction-1"))

        await publish()

        assert observed == ["sql.begin"]
        assert "Event handler failed" in caplog.messages

    @pytest.mark.anyio
    async def test_handler_failure_records_secret_safe_diagnostics(self) -> None:
        dispatcher = EventDispatcher()
        diagnostics = DiagnosticsCapability()

        async def failing_handler(_event: Event) -> None:
            raise RuntimeError("password=should-not-appear")

        dispatcher.subscribe(EVT_SQL, failing_handler)

        @scope(EVT_SQL)
        async def publish() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="secret-value"))

        async with diagnostic_context(
            diagnostics,
            kind="event_test",
            description="Event handler failure",
            level="trace",
        ):
            await publish()

        events = diagnostics.snapshots(EVT_EVENTS_ERRORS)[0].events

        handler_failure = next(
            event for event in events if event["name"] == "errors.handler.failed"
        )
        attributes = cast(dict[str, object], handler_failure["attributes"])
        assert attributes["error_type"] == "RuntimeError"
        assert "secret-value" not in str(handler_failure)
        assert "should-not-appear" not in str(handler_failure)

    @pytest.mark.anyio
    async def test_handler_failure_is_retained_at_default_diagnostics_settings(
        self,
    ) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.events",),
                        "deployment_environment": "local",
                    },
                    "wybra.diagnostics": {"events_enabled": True},
                }
            ),
        )
        try:
            diagnostics = site.require_capability(DiagnosticsCapability)
            dispatcher = site.require_capability(EventsCapability)

            async def failing_handler(_event: Event) -> None:
                raise RuntimeError("expected handler failure")

            dispatcher.subscribe(EVT_EVENTS, failing_handler)

            @scope(EVT_EVENTS)
            async def publish() -> None:
                await dispatcher.publish(
                    TransactionStarted(transaction_id="transaction-1")
                )

            async with diagnostic_context(
                diagnostics,
                kind="event_test",
                description="Default diagnostics settings",
            ):
                await publish()

            events = diagnostics.snapshots(EVT_EVENTS_ERRORS)[0].events
        finally:
            await site.close()

        assert [event["name"] for event in events] == ["errors.handler.failed"]
        assert events[0]["level"] == "info"

    @pytest.mark.anyio
    async def test_dispatch_diagnostic_reports_failed_handlers(self) -> None:
        dispatcher = EventDispatcher()
        diagnostics = DiagnosticsCapability()

        async def failing_handler(_event: Event) -> None:
            raise RuntimeError("expected handler failure")

        dispatcher.subscribe(EVT_EVENTS, failing_handler)

        @scope(EVT_EVENTS)
        async def publish() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="transaction-1"))

        async with diagnostic_context(
            diagnostics,
            kind="event_test",
            description="Dispatch result",
            level="trace",
        ):
            await publish()

        events = diagnostics.snapshots(EVT_EVENTS)[0].events
        dispatch = next(event for event in events if event["name"] == "dispatch")

        assert dispatch["result"] == "error"

    @pytest.mark.anyio
    async def test_operation_observations_do_not_control_success_or_failure(
        self,
    ) -> None:
        dispatcher = EventDispatcher()
        observed: list[Event] = []

        async def failing_pre_handler(event: Event) -> None:
            if isinstance(event, CacheInvalidationStarted):
                raise RuntimeError("observation only")

        async def record_handler(event: Event) -> None:
            observed.append(event)

        dispatcher.subscribe(EVT_EVENTS, failing_pre_handler)
        dispatcher.subscribe(EVT_EVENTS, record_handler)

        @scope(EVT_EVENTS)
        async def successful_operation() -> str:
            async with observe_operation(
                dispatcher,
                CacheInvalidationStarted(owner="template", key="article:1"),
                lambda outcome: CacheInvalidated(outcome=outcome),
            ):
                return "completed"

        @scope(EVT_EVENTS)
        async def failed_operation() -> None:
            async with observe_operation(
                dispatcher,
                CacheInvalidationStarted(owner="template", key="article:1"),
                lambda outcome: CacheInvalidated(outcome=outcome),
            ):
                raise LookupError("business failure")

        assert await successful_operation() == "completed"
        with pytest.raises(LookupError, match="business failure"):
            await failed_operation()

        outcomes = [
            event.outcome for event in observed if isinstance(event, CacheInvalidated)
        ]
        assert outcomes == [
            EventOutcome(succeeded=True),
            EventOutcome(succeeded=False, error_type="LookupError"),
        ]

    @pytest.mark.anyio
    async def test_operation_runs_when_pre_operation_dispatch_fails(self) -> None:
        completed: list[str] = []

        @scope(EVT_EVENTS)
        async def operation() -> None:
            async with observe_operation(
                cast(EventsCapability, UnavailableEvents()),
                CacheInvalidationStarted(owner="template", key="article:1"),
                lambda outcome: CacheInvalidated(outcome=outcome),
            ):
                completed.append("completed")

        await operation()

        assert completed == ["completed"]

    @pytest.mark.anyio
    async def test_delivery_does_not_require_diagnostics(self) -> None:
        dispatcher = EventDispatcher()
        observed: list[str] = []

        async def handler(event: Event) -> None:
            observed.append(str(event.scope))

        dispatcher.subscribe(EVT_EVENTS, handler)

        @scope(EVT_EVENTS)
        async def publish() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="transaction-1"))

        await publish()

        assert observed == ["events.begin"]

    def test_rejects_synchronous_handlers(self) -> None:
        def handler(_event: Event) -> None:
            pass

        with pytest.raises(TypeError, match="async"):
            EventDispatcher().subscribe(EVT_SQL, cast(EventHandler, handler))

    def test_accepts_an_object_with_an_async_call_method(self) -> None:
        class Handler:
            async def __call__(self, _event: Event) -> None:
                pass

        EventDispatcher().subscribe(EVT_SQL, Handler())


class TestEventsSiteIntegration:
    @pytest.mark.anyio
    async def test_events_module_provides_the_dispatch_capability(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.events",),
                        "deployment_environment": "local",
                    }
                }
            ),
        )
        try:
            assert isinstance(
                site.require_capability(EventsCapability),
                EventDispatcher,
            )
        finally:
            await site.close()

    @pytest.mark.anyio
    async def test_module_setup_can_register_an_event_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: list[str] = []
        handler_module = ModuleType("test_event_handler_module")

        async def setup_handler_module(site: Site) -> None:
            capability = site.require_capability(EventsCapability)

            async def handler(event: Event) -> None:
                observed.append(str(event.scope))

            capability.subscribe(EVT_SQL, handler)

        handler_module.__dict__["setup_site"] = setup_handler_module
        monkeypatch.setitem(sys.modules, handler_module.__name__, handler_module)

        def module_loader(name: str) -> ModuleType:
            if name == handler_module.__name__:
                return handler_module
            return import_module(name)

        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.events", handler_module.__name__),
                        "deployment_environment": "local",
                    }
                }
            ),
            module_loader=module_loader,
        )
        try:
            dispatcher = site.require_capability(EventsCapability)

            @scope(EVT_SQL)
            async def publish() -> None:
                await dispatcher.publish(
                    TransactionStarted(transaction_id="transaction-1")
                )

            await publish()
        finally:
            await site.close()

        assert observed == ["sql.begin"]
