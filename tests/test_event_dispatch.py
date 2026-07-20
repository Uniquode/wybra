from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Annotated, ClassVar, cast

import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import Response
from jinja2 import TemplateNotFound

from wybra.auth.events import publish_account_lifecycle
from wybra.config import MappingConfigSource
from wybra.db.events import DatabaseStatementEvent
from wybra.db.persistence import close_database, create_database
from wybra.diagnostics import (
    DiagnosticsCapability,
    diagnostic_context,
)
from wybra.events import (
    BEGIN,
    CAPABILITY,
    EVT_ACCOUNT,
    EVT_EVENTS,
    EVT_EVENTS_ERRORS,
    EVT_FORM,
    EVT_REQUEST,
    EVT_ROUTE,
    EVT_SITE,
    EVT_SQL,
    EVT_TEMPLATE,
    EVT_VIEW,
    TRANSACTION,
    AccountLifecycleEvent,
    CapabilityProvidedEvent,
    CapabilityResolvedEvent,
    CapabilityUnavailableEvent,
    Event,
    EventDispatcher,
    EventHandler,
    EventOutcome,
    EventsCapability,
    FormValidationCompletedEvent,
    ModulePostSetupEvent,
    ModuleSetupEvent,
    RequestCompletedEvent,
    RouteDispatchedEvent,
    SiteLifecycleEvent,
    TemplateRenderCompletedEvent,
    ViewCompletedEvent,
    current_scope,
    event_segment,
    extend,
    observe_operation,
    scope,
)
from wybra.forms import Form, TextField
from wybra.site import Site, SiteCapabilityError, start
from wybra.template import DefaultTemplateCapability
from wybra.testing import WybraTestClient
from wybra.views import View, ViewRouter


@dataclass(frozen=True, slots=True)
class TransactionStarted(Event):
    kind: ClassVar = BEGIN
    transaction_id: str


class ProxyTestCapability:
    pass


class UnavailableProxyCapability:
    pass


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


class ObservedForm(Form):
    title = TextField(required=True)


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
    async def test_external_cancellation_of_event_publication_is_preserved(
        self,
    ) -> None:
        dispatcher = EventDispatcher()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def waiting_handler(event: Event) -> None:
            handler_started.set()
            await release_handler.wait()

        dispatcher.subscribe(EVT_SQL, waiting_handler)

        @scope(EVT_SQL)
        async def publish() -> None:
            await dispatcher.publish(TransactionStarted(transaction_id="transaction-1"))

        publication = asyncio.create_task(publish())
        await handler_started.wait()
        publication.cancel()

        with pytest.raises(asyncio.CancelledError):
            await publication

        release_handler.set()
        await asyncio.sleep(0)

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
                        "modules": (),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
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
    async def test_core_events_capability_discards_events_when_disabled(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": (),
                        "deployment_environment": "local",
                    }
                }
            ),
        )
        try:
            dispatcher = site.require_capability(EventsCapability)
            observed: list[Event] = []

            async def handler(event: Event) -> None:
                observed.append(event)

            dispatcher.subscribe(EVT_SQL, handler)

            @scope(EVT_SQL)
            async def publish() -> None:
                await dispatcher.publish(
                    TransactionStarted(transaction_id="transaction-1")
                )

            await publish()
        finally:
            await site.close()

        assert observed == []

    @pytest.mark.anyio
    async def test_core_events_capability_dispatches_when_enabled(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": (),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
                    "wybra.diagnostics": {"events_enabled": True},
                }
            ),
        )
        try:
            dispatcher = site.require_capability(EventsCapability)
            observed: list[Event] = []

            async def handler(event: Event) -> None:
                observed.append(event)

            dispatcher.subscribe(EVT_SQL, handler)

            @scope(EVT_SQL)
            async def publish() -> None:
                await dispatcher.publish(
                    TransactionStarted(transaction_id="transaction-1")
                )

            await publish()
        finally:
            await site.close()

        assert [str(event.scope) for event in observed] == ["sql.begin"]

    @pytest.mark.anyio
    async def test_diagnostics_projects_selected_events_passively(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": (),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
                    "wybra.diagnostics": {
                        "events_enabled": True,
                        "event_scopes": "sql",
                        "level": "trace",
                    },
                }
            ),
        )
        try:
            dispatcher = site.require_capability(EventsCapability)
            diagnostics = site.require_capability(DiagnosticsCapability)

            @scope(EVT_SQL)
            async def publish() -> None:
                await dispatcher.publish(
                    TransactionStarted(transaction_id="transaction-1")
                )

            async with diagnostic_context(
                diagnostics,
                kind="event_test",
                description="Passive event projection",
                level="trace",
            ):
                await publish()

            events = diagnostics.snapshots(EVT_SQL)[0].events
        finally:
            await site.close()

        assert [event["name"] for event in events] == ["begin"]
        attributes = cast(dict[str, object], events[0]["attributes"])
        assert attributes == {
            "event_type": "test_event_dispatch.TransactionStarted",
        }

    @pytest.mark.anyio
    async def test_outer_middleware_publishes_request_lifecycle_events(self) -> None:
        app = FastAPI()

        @app.get("/events", name="events")
        async def events_endpoint() -> dict[str, bool]:
            return {"ok": True}

        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(EVT_REQUEST, handler)
        try:
            with WybraTestClient(app) as client:
                response = client.get("/events")
        finally:
            await site.close()

        assert response.json() == {"ok": True}
        assert [str(event.scope) for event in observed] == [
            "request.started",
            "request.completed",
        ]
        completed = observed[-1]
        assert isinstance(completed, RequestCompletedEvent)
        assert completed.status_code == 200
        assert completed.route_name == "events"
        assert completed.error_type is None

    @pytest.mark.anyio
    async def test_request_failure_publishes_one_completion_without_swallowing_error(
        self,
    ) -> None:
        app = FastAPI()

        @app.get("/failure", name="failure")
        async def failure_endpoint() -> None:
            raise RuntimeError("expected failure")

        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(EVT_REQUEST, handler)
        try:
            with WybraTestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/failure")
        finally:
            await site.close()

        assert response.status_code == 500
        assert [str(event.scope) for event in observed] == [
            "request.started",
            "request.completed",
        ]
        completed = observed[-1]
        assert isinstance(completed, RequestCompletedEvent)
        assert completed.status_code is None
        assert completed.error_type == "RuntimeError"

    @pytest.mark.anyio
    async def test_cancelling_request_observer_cannot_cancel_response(self) -> None:
        app = FastAPI()

        @app.get("/events")
        async def events_endpoint() -> dict[str, bool]:
            return {"ok": True}

        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )

        async def cancelling_handler(event: Event) -> None:
            raise asyncio.CancelledError()

        site.require_capability(EventsCapability).subscribe(
            EVT_REQUEST, cancelling_handler
        )
        try:
            with WybraTestClient(app) as client:
                response = client.get("/events")
        finally:
            await site.close()

        assert response.json() == {"ok": True}

    @pytest.mark.anyio
    async def test_request_events_do_not_consume_multipart_uploads(self) -> None:
        app = FastAPI()

        @app.post("/upload", name="upload")
        async def upload_endpoint(
            upload: Annotated[UploadFile, File()],
        ) -> dict[str, str]:
            return {"content": (await upload.read()).decode()}

        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(EVT_REQUEST, handler)
        try:
            with WybraTestClient(app) as client:
                response = client.post(
                    "/upload",
                    files={"upload": ("message.txt", b"upload contents", "text/plain")},
                )
        finally:
            await site.close()

        assert response.json() == {"content": "upload contents"}
        assert [str(event.scope) for event in observed] == [
            "request.started",
            "request.completed",
        ]
        completed = observed[-1]
        assert isinstance(completed, RequestCompletedEvent)
        assert completed.route_name == "upload"

    @pytest.mark.anyio
    async def test_request_events_wrap_existing_middleware(self) -> None:
        app = FastAPI()
        order: list[str] = []

        @app.middleware("http")
        async def existing_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            order.append(f"middleware.before:{current_scope()}")
            response = await call_next(request)
            order.append(f"middleware.after:{current_scope()}")
            return response

        @app.get("/ordered")
        async def ordered_endpoint() -> dict[str, bool]:
            order.append(f"endpoint:{current_scope()}")
            return {"ok": True}

        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )

        async def handler(event: Event) -> None:
            order.append(f"event:{event.scope}")

        site.require_capability(EventsCapability).subscribe(EVT_REQUEST, handler)
        try:
            with WybraTestClient(app) as client:
                response = client.get("/ordered")
        finally:
            await site.close()

        assert response.json() == {"ok": True}
        assert order == [
            "event:request.started",
            "middleware.before:request",
            "endpoint:request",
            "middleware.after:request",
            "event:request.completed",
        ]

    @pytest.mark.anyio
    async def test_class_based_view_dispatch_publishes_route_and_outcome(self) -> None:
        app = FastAPI()
        router = ViewRouter()

        @router.view("/observed", name="observed")
        class ObservedView(View):
            async def get(self, _request: Request) -> dict[str, bool]:
                return {"ok": True}

        app.include_router(router)
        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        events = site.require_capability(EventsCapability)
        events.subscribe(EVT_ROUTE, handler)
        events.subscribe(EVT_VIEW, handler)
        try:
            with WybraTestClient(app) as client:
                response = client.get("/observed")
        finally:
            await site.close()

        assert response.json() == {"ok": True}
        assert [str(event.scope) for event in observed] == [
            "route.dispatch",
            "view.completed",
        ]
        route_event, view_event = observed
        assert isinstance(route_event, RouteDispatchedEvent)
        assert route_event.method == "GET"
        assert route_event.route_name == "observed"
        assert route_event.view_type == "ObservedView"
        assert isinstance(view_event, ViewCompletedEvent)
        assert view_event.status_code == 200
        assert view_event.error_type is None

    @pytest.mark.anyio
    async def test_class_based_view_error_is_not_changed_by_observation(self) -> None:
        app = FastAPI()
        router = ViewRouter()

        @router.view("/failing", name="failing")
        class FailingView(View):
            async def get(self, _request: Request) -> None:
                raise RuntimeError("expected failure")

        app.include_router(router)
        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(EVT_VIEW, handler)
        try:
            with WybraTestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/failing")
        finally:
            await site.close()

        assert response.status_code == 500
        assert len(observed) == 1
        event = observed[0]
        assert isinstance(event, ViewCompletedEvent)
        assert event.status_code is None
        assert event.error_type == "RuntimeError"

    @pytest.mark.anyio
    async def test_template_rendering_publishes_success_and_failure(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "page.html").write_text("Hello {{ name }}", encoding="utf-8")
        events = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        events.subscribe(EVT_TEMPLATE, handler)
        templates = DefaultTemplateCapability(template_root=tmp_path, events=events)

        assert await templates.render_template("page.html", {"name": "Wybra"}) == (
            "Hello Wybra"
        )
        with pytest.raises(TemplateNotFound):
            await templates.render_template("missing.html", {"secret": "not exposed"})

        assert [str(event.scope) for event in observed] == [
            "template.render.completed",
            "template.render.completed",
        ]
        succeeded, failed = observed
        assert isinstance(succeeded, TemplateRenderCompletedEvent)
        assert succeeded.template_name == "page.html"
        assert succeeded.error_type is None
        assert isinstance(failed, TemplateRenderCompletedEvent)
        assert failed.template_name == "missing.html"
        assert failed.error_type == "TemplateNotFound"

    @pytest.mark.anyio
    async def test_form_validation_publishes_no_submitted_values(self) -> None:
        events = EventDispatcher()
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        events.subscribe(EVT_FORM, handler)
        form = ObservedForm(events=events)

        result = await form.parse({"title": ""})

        assert result.is_valid is False
        assert len(observed) == 1
        event = observed[0]
        assert isinstance(event, FormValidationCompletedEvent)
        assert str(event.scope) == "form.validation.completed"
        assert event.field_count == 1
        assert event.invalid_field_count == 1
        assert event.valid is False
        assert not hasattr(event, "values")
        assert not hasattr(event, "errors")

    @pytest.mark.anyio
    async def test_account_lifecycle_masks_email_and_retains_user_uuid(self) -> None:
        app = FastAPI()
        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(EVT_ACCOUNT, handler)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/signup",
                "headers": [],
                "app": app,
            }
        )
        try:
            await publish_account_lifecycle(
                request,
                operation="signup",
                outcome="succeeded",
                user_id="018f261d-0000-7000-8000-000000000001",
                email="username@example.com",
            )
        finally:
            await site.close()

        assert len(observed) == 1
        event = observed[0]
        assert isinstance(event, AccountLifecycleEvent)
        assert event.user_id == "018f261d-0000-7000-8000-000000000001"
        assert event.masked_email == "u**@example**"
        assert "username@example.com" not in repr(event)

    @pytest.mark.anyio
    async def test_capability_proxy_resolution_publishes_available_and_unavailable(
        self,
    ) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        site.require_capability(EventsCapability).subscribe(
            EVT_SITE(CAPABILITY), handler
        )
        available_proxy = site.capability_proxy(ProxyTestCapability)
        unavailable_proxy = site.capability_proxy(UnavailableProxyCapability)
        capability = ProxyTestCapability()
        site.provide_capability(ProxyTestCapability, capability)
        try:
            assert await available_proxy.require() is capability
            assert await unavailable_proxy.optional() is None
        finally:
            await site.close()

        assert [str(event.scope) for event in observed] == [
            "site.capability.resolved",
            "site.capability.unavailable",
        ]
        assert isinstance(observed[0], CapabilityResolvedEvent)
        assert isinstance(observed[1], CapabilityUnavailableEvent)

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
                        "modules": (handler_module.__name__,),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
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

    @pytest.mark.anyio
    async def test_module_hooks_publish_setup_and_post_setup_outcomes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: list[tuple[str, str, str, str]] = []
        observer_module = ModuleType("event_observer_module")
        subject_module = ModuleType("event_subject_module")

        async def setup_observer(site: Site) -> None:
            dispatcher = site.require_capability(EventsCapability)

            async def handler(event: Event) -> None:
                if not isinstance(event, ModuleSetupEvent | ModulePostSetupEvent):
                    return
                observed.append(
                    (
                        str(event.scope),
                        type(event).__name__,
                        event.module,
                        event.outcome,
                    )
                )

            dispatcher.subscribe(EVT_SITE, handler)

        async def setup_subject(_site: Site) -> None:
            return None

        async def post_setup_subject(_site: Site) -> None:
            return None

        observer_module.__dict__["setup_site"] = setup_observer
        subject_module.__dict__["setup_site"] = setup_subject
        subject_module.__dict__["post_setup_site"] = post_setup_subject
        modules = {
            observer_module.__name__: observer_module,
            subject_module.__name__: subject_module,
        }
        for module_name, module in modules.items():
            monkeypatch.setitem(sys.modules, module_name, module)

        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": tuple(modules),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
                    "wybra.diagnostics": {"events_enabled": True},
                }
            ),
            module_loader=modules.__getitem__,
        )
        await site.close()

        assert observed == [
            (
                "site.module.setup",
                "ModuleSetupEvent",
                observer_module.__name__,
                "succeeded",
            ),
            (
                "site.module.setup",
                "ModuleSetupEvent",
                subject_module.__name__,
                "started",
            ),
            (
                "site.module.setup",
                "ModuleSetupEvent",
                subject_module.__name__,
                "succeeded",
            ),
            (
                "site.module.post_setup",
                "ModulePostSetupEvent",
                subject_module.__name__,
                "started",
            ),
            (
                "site.module.post_setup",
                "ModulePostSetupEvent",
                subject_module.__name__,
                "succeeded",
            ),
        ]

    @pytest.mark.anyio
    async def test_module_hook_failure_publishes_before_startup_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: list[tuple[str, str, str | None]] = []
        observer_module = ModuleType("failing_event_observer_module")
        failing_module = ModuleType("failing_event_subject_module")

        async def setup_observer(site: Site) -> None:
            dispatcher = site.require_capability(EventsCapability)

            async def handler(event: Event) -> None:
                if not isinstance(event, ModuleSetupEvent | ModulePostSetupEvent):
                    return
                observed.append(
                    (
                        str(event.scope),
                        event.outcome,
                        event.error_type,
                    )
                )

            dispatcher.subscribe(EVT_SITE, handler)

        async def setup_failing(_site: Site) -> None:
            raise RuntimeError("expected failure")

        observer_module.__dict__["setup_site"] = setup_observer
        failing_module.__dict__["setup_site"] = setup_failing
        modules = {
            observer_module.__name__: observer_module,
            failing_module.__name__: failing_module,
        }
        for module_name, module in modules.items():
            monkeypatch.setitem(sys.modules, module_name, module)

        with pytest.raises(SiteCapabilityError, match="error_type=RuntimeError"):
            await start(
                FastAPI(),
                config_source=MappingConfigSource(
                    {
                        "app": {
                            "modules": tuple(modules),
                            "deployment_environment": "local",
                        },
                        "wybra.events": {"enabled": True},
                    }
                ),
                module_loader=modules.__getitem__,
            )

        assert observed[-2:] == [
            ("site.module.setup", "started", None),
            ("site.module.setup", "failed", "RuntimeError"),
        ]

    @pytest.mark.anyio
    async def test_site_lifecycle_and_capability_setup_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: list[Event] = []
        observer_module = ModuleType("site_lifecycle_event_observer")
        capability_module = ModuleType("site_lifecycle_event_capability")

        async def setup_observer(site: Site) -> None:
            async def handler(event: Event) -> None:
                observed.append(event)

            site.require_capability(EventsCapability).subscribe(EVT_SITE, handler)

        async def setup_capability(site: Site) -> None:
            site.provide_capability(ProxyTestCapability, ProxyTestCapability())

        observer_module.__dict__["setup_site"] = setup_observer
        capability_module.__dict__["setup_site"] = setup_capability
        modules = {
            observer_module.__name__: observer_module,
            capability_module.__name__: capability_module,
        }
        for module_name, module in modules.items():
            monkeypatch.setitem(sys.modules, module_name, module)

        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": tuple(modules),
                        "deployment_environment": "local",
                    },
                    "wybra.events": {"enabled": True},
                    "wybra.diagnostics": {"events_enabled": True},
                }
            ),
            module_loader=modules.__getitem__,
        )
        await site.close()

        assert any(
            isinstance(event, CapabilityProvidedEvent)
            and event.capability_type.endswith("ProxyTestCapability")
            for event in observed
        )
        assert any(
            isinstance(event, CapabilityProvidedEvent)
            and event.capability_type.endswith("DiagnosticsCapability")
            for event in observed
        )
        assert [
            (event.phase, event.error_count)
            for event in observed
            if isinstance(event, SiteLifecycleEvent)
        ] == [("startup", 0), ("shutdown", 0)]

    @pytest.mark.anyio
    async def test_cancelling_shutdown_observer_cannot_abort_site_close(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )

        async def cancelling_handler(event: Event) -> None:
            if isinstance(event, SiteLifecycleEvent) and event.phase == "shutdown":
                raise asyncio.CancelledError()

        site.require_capability(EventsCapability).subscribe(
            EVT_SITE, cancelling_handler
        )

        await site.close()

        assert not site.has_capability(EventsCapability)

    @pytest.mark.anyio
    async def test_database_request_chain_reaches_events_without_diagnostics(
        self,
    ) -> None:
        app = FastAPI()
        site = await start(
            app,
            config_source=MappingConfigSource(
                {
                    "app": {"modules": (), "deployment_environment": "local"},
                    "wybra.events": {"enabled": True},
                }
            ),
        )
        events = site.require_capability(EventsCapability)
        observed: list[Event] = []

        async def handler(event: Event) -> None:
            observed.append(event)

        events.subscribe(EVT_REQUEST, handler)
        events.subscribe(EVT_SQL, handler)
        database = await create_database(
            "sqlite://:memory:",
            modules=("wybra.sessions",),
            events=events,
        )

        @app.get("/database")
        async def database_route() -> dict[str, int]:
            with database.context:
                await database.connection().execute_query("select 1")
            return {"status": 1}

        try:
            async with WybraTestClient(app) as client:
                response = await client.get("/database")
        finally:
            await close_database(database)
            await site.close()

        assert response.json() == {"status": 1}
        scopes = [str(event.scope) for event in observed]
        assert scopes.index("request.started") < scopes.index("sql.statement")
        assert scopes.index("sql.statement") < scopes.index("request.completed")
        assert any(isinstance(event, DatabaseStatementEvent) for event in observed)
