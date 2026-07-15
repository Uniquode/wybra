from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from tortoise.transactions import in_transaction

from wybra import start_site
from wybra.config import MappingConfigSource
from wybra.core.logging import TRACE_LEVEL, configure_runtime_logging, get_trace_logger
from wybra.db.persistence import close_database, create_database
from wybra.diagnostics import (
    backend_operation_diagnostics,
    current_diagnostics,
    request_diagnostics,
    template_render_diagnostics,
    trace,
)
from wybra.diagnostics.context import (
    record_sql_query,
    reset_current_diagnostics,
    set_current_diagnostics,
)
from wybra.diagnostics.events import RequestDiagnostics
from wybra.diagnostics.logging import emit_request_diagnostics
from wybra.diagnostics.tortoise import instrument_tortoise_context
from wybra.site import start
from wybra.template.capabilities import DefaultTemplateCapability
from wybra.testing import WybraTestClient


@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in original_handlers:
            handler.close()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def _diagnostic_app_config(
    *,
    diagnostics_level: str = "trace",
    logging_bridge: bool = False,
) -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {
                "modules": (),
                "deployment_environment": "local",
            },
            "wybra.diagnostics": {
                "enabled": True,
                "level": diagnostics_level,
                "logging_bridge": logging_bridge,
                "slow_sql_threshold_seconds": 0.001,
            },
        }
    )


def test_trace_level_is_registered_and_logger_trace_emits(caplog) -> None:
    logger = get_trace_logger("wybra.tests.trace")
    logging.getLogger("wybra.tests.trace").setLevel(TRACE_LEVEL)

    with caplog.at_level(TRACE_LEVEL, logger="wybra.tests.trace"):
        logger.trace("trace message")

    assert logging.getLevelName(TRACE_LEVEL) == "TRACE"
    assert [(record.levelno, record.message) for record in caplog.records] == [
        (TRACE_LEVEL, "trace message")
    ]


def test_trace_logging_configuration_is_opt_in() -> None:
    configure_runtime_logging(
        config={
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "console": {
                    "class": "logging.NullHandler",
                    "level": "TRACE",
                }
            },
            "root": {"level": "INFO", "handlers": ["console"]},
        }
    )

    assert logging.getLogger().level == logging.INFO
    assert logging.getLevelName(TRACE_LEVEL) == "TRACE"


def test_diagnostics_redacts_sensitive_attributes() -> None:
    diagnostics = RequestDiagnostics(method="GET", path="/", level="trace")
    token = set_current_diagnostics(diagnostics)
    try:
        trace(
            "request",
            "headers",
            attributes={
                "authorisation": "Bearer secret",
                "cookie": "session=secret",
                "safe": "visible",
            },
        )
    finally:
        reset_current_diagnostics(token)

    assert diagnostics.events[0].attributes == {
        "authorisation": "[redacted]",
        "cookie": "[redacted]",
        "safe": "visible",
    }
    assert current_diagnostics() is None


def test_request_diagnostics_collect_without_logging_bridge(caplog) -> None:
    app = FastAPI(lifespan=start_site(config_source=_diagnostic_app_config()))

    @app.get("/status", name="status")
    async def status(request: Request) -> dict[str, object]:
        trace("custom", "inside_request", attributes={"safe": "yes"})
        diagnostics = request_diagnostics(request)
        assert diagnostics is not None
        return {"events": len(diagnostics.events)}

    caplog.set_level(TRACE_LEVEL, logger="wybra.diagnostics")

    with WybraTestClient(app) as client:
        response = client.get("/status")

    assert response.json()["events"] >= 1
    assert [
        record
        for record in caplog.records
        if record.name.startswith("wybra.diagnostics")
    ] == []


def test_request_diagnostics_logging_bridge_uses_dedicated_logger(caplog) -> None:
    diagnostics = RequestDiagnostics(method="GET", path="/status", level="trace")
    diagnostics.record_event(
        "trace",
        "custom",
        "inside_request",
        attributes={"token": "secret"},
    )
    diagnostics.finish(
        route_name="status",
        status_code=200,
        exception_type=None,
        duration_seconds=0.01,
    )

    with caplog.at_level(TRACE_LEVEL, logger="wybra.diagnostics"):
        emit_request_diagnostics(diagnostics)

    diagnostics_records = [
        record for record in caplog.records if record.name == "wybra.diagnostics"
    ]
    assert diagnostics_records
    bridged = [
        record.wybra_diagnostics
        for record in diagnostics_records
        if getattr(record, "wybra_diagnostics", {}).get("kind") == "event"
    ]
    assert any(event["category"] == "custom" for event in bridged)
    assert any(event["attributes"].get("token") == "[redacted]" for event in bridged)


def test_diagnostics_recording_failures_do_not_escape(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_record(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("diagnostics failed")

    monkeypatch.setattr(RequestDiagnostics, "record_template_render", broken_record)
    diagnostics = RequestDiagnostics(method="GET", path="/", level="trace")
    token = set_current_diagnostics(diagnostics)
    try:
        with caplog.at_level(logging.DEBUG, logger="wybra.diagnostics.context"):
            with template_render_diagnostics("page.html"):
                pass

            with pytest.raises(ValueError, match="render failed"):
                with template_render_diagnostics("page.html"):
                    raise ValueError("render failed")
    finally:
        reset_current_diagnostics(token)

    assert "Diagnostics recording failed" in caplog.text


@pytest.mark.anyio
async def test_backend_operation_diagnostics_records_error_result() -> None:
    diagnostics = RequestDiagnostics(method="GET", path="/", level="trace")
    token = set_current_diagnostics(diagnostics)
    try:
        with pytest.raises(ValueError, match="operation failed"):
            async with backend_operation_diagnostics(
                "backend",
                "operation",
                attributes=lambda: {"safe": "yes"},
            ):
                raise ValueError("operation failed")
    finally:
        reset_current_diagnostics(token)

    assert [
        (event.category, event.name, event.result, event.attributes)
        for event in diagnostics.events
    ] == [("backend", "operation", "error", {"safe": "yes"})]


def test_sql_template_and_backend_diagnostics_are_collected(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text("Hello {{ name }}", encoding="utf-8")
    templates = DefaultTemplateCapability(template_root=tmp_path)
    app = FastAPI(lifespan=start_site(config_source=_diagnostic_app_config()))

    @app.get("/work", name="work")
    async def work(request: Request) -> dict[str, object]:
        record_sql_query("select 1", duration_seconds=0.001)
        templates.render_template("page.html", {"name": "diagnostics"})
        diagnostics = request_diagnostics(request)
        assert diagnostics is not None
        return {
            "sql": diagnostics.sql_query_count,
            "templates": diagnostics.template_render_count,
            "backend": diagnostics.backend_operation_count,
        }

    with WybraTestClient(app) as client:
        response = client.get("/work")

    assert response.json()["sql"] == 1
    assert response.json()["templates"] == 1
    assert response.json()["backend"] >= 1


@pytest.mark.anyio
async def test_tortoise_instrumentation_is_idempotent() -> None:
    database = await create_database(
        "sqlite://:memory:",
        modules=("wybra.sessions",),
    )
    instrument_tortoise_context(database.context)
    diagnostics = RequestDiagnostics(method="GET", path="/", level="trace")
    token = set_current_diagnostics(diagnostics)

    try:
        with database.context:
            await database.connection().execute_query("select 1")
    finally:
        reset_current_diagnostics(token)
        await close_database(database)

    assert diagnostics.sql_query_count == 1


@pytest.mark.anyio
async def test_tortoise_instrumentation_records_transaction_queries() -> None:
    database = await create_database(
        "sqlite://:memory:",
        modules=("wybra.sessions",),
    )
    diagnostics = RequestDiagnostics(method="GET", path="/", level="trace")
    token = set_current_diagnostics(diagnostics)

    try:
        with database.context:
            async with in_transaction("default") as connection:
                await connection.execute_query("select 1")
                async with in_transaction(connection.connection_name) as savepoint:
                    await savepoint.execute_query("select 2")
    finally:
        reset_current_diagnostics(token)
        await close_database(database)

    assert diagnostics.sql_query_count == 2


@pytest.mark.anyio
async def test_diagnostics_middleware_wraps_module_middleware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "diagnostics_order_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text(
        "from collections.abc import Awaitable, Callable\n"
        "from fastapi import Request\n"
        "from fastapi.responses import Response\n\n"
        "async def setup_site(site):\n"
        "    @site.app.middleware('http')\n"
        "    async def module_middleware(\n"
        "        request: Request,\n"
        "        call_next: Callable[[Request], Awaitable[Response]],\n"
        "    ) -> Response:\n"
        "        response = await call_next(request)\n"
        "        response.headers['X-Middleware-Order'] = 'module'\n"
        "        return response\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    app = FastAPI()

    @app.get("/status")
    async def status(request: Request) -> dict[str, object]:
        return {"diagnostics": request_diagnostics(request) is not None}

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "modules": ("diagnostics_order_app",),
                    "deployment_environment": "local",
                },
                "wybra.diagnostics": {
                    "enabled": True,
                    "level": "trace",
                },
            }
        ),
    )

    try:
        # Starlette inserts newly registered middleware first; registering
        # diagnostics after module setup makes it the outer user middleware.
        assert app.user_middleware[0].kwargs["dispatch"].__name__ == (
            "diagnostics_middleware"
        )

        with WybraTestClient(app) as client:
            response = client.get("/status")
    finally:
        await site.close()

    assert response.json() == {"diagnostics": True}
    assert response.headers["X-Middleware-Order"] == "module"
