from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse

from wybra import start_site
from wybra.api import ApiCapability
from wybra.config import ConfigService, MappingConfigSource
from wybra.errors.capabilities import ErrorHandlingCapability
from wybra.errors.handlers import (
    ErrorPresentation,
    _api_error_code,
    _resolve_error_response_kind,
    register_error_handlers,
)
from wybra.site import Site
from wybra.testing import WybraTestClient


class CustomApiCapability:
    def is_api_request(self, request, *, route_type=None) -> bool:
        return request.url.path.startswith("/api")

    def response(self, data, *, status_code=200, headers=None, metadata=None):
        return JSONResponse({"custom_data": data}, status_code=status_code)

    def paged_response(
        self,
        items,
        *,
        paging,
        status_code=200,
        headers=None,
        metadata=None,
    ):
        return JSONResponse({"custom_items": list(items)}, status_code=status_code)

    def error_response(self, error, *, status_code, headers=None):
        return JSONResponse(
            {
                "custom_error": error.code,
                "custom_message": error.message,
            },
            status_code=status_code,
            headers=headers,
        )

    def status_response(
        self,
        *,
        status_code,
        message=None,
        headers=None,
        metadata=None,
    ):
        return JSONResponse({"custom_status": message}, status_code=status_code)

    def validation_error_response(
        self,
        errors,
        *,
        status_code=422,
        headers=None,
    ):
        return JSONResponse({"custom_errors": list(errors)}, status_code=status_code)

    def streaming_response(
        self,
        body,
        *,
        status_code=200,
        headers=None,
        media_type=None,
    ):
        return StreamingResponse(
            body,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )


class CustomErrorCapability:
    def response_for_exception(self, request, exc):
        return JSONResponse(
            {
                "custom_error": type(exc).__name__,
                "path": request.url.path,
            },
            status_code=599,
        )


def test_errors_are_translated_through_error_handling_capability() -> None:
    app = FastAPI()
    site = Site(
        app=app,
        config=ConfigService([MappingConfigSource({"app": {"modules": []}})]),
    )
    app.state.site = site
    site.provide_capability(ErrorHandlingCapability, CustomErrorCapability())
    register_error_handlers(app)

    @app.get("/fail")
    async def fail() -> Response:
        raise RuntimeError("boom")

    with WybraTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail")

    assert response.status_code == 599
    assert response.json() == {"custom_error": "RuntimeError", "path": "/fail"}


@pytest.mark.parametrize(
    ("debug", "includes_traceback"),
    ((True, True), (False, False)),
)
def test_debug_mode_controls_error_response(
    debug: bool,
    includes_traceback: bool,
) -> None:
    app = FastAPI(
        lifespan=start_site(
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.errors",),
                        "deployment_environment": "local",
                        "debug": debug,
                    }
                }
            )
        )
    )

    @app.get("/fail")
    async def fail() -> Response:
        raise RuntimeError("boom")

    with WybraTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail")

    assert response.status_code == 500
    assert ("Traceback" in response.text) is includes_traceback
    if includes_traceback:
        assert "RuntimeError: boom" in response.text


def test_api_errors_are_rendered_through_api_capability() -> None:
    app = FastAPI()
    site = Site(
        app=app,
        config=ConfigService([MappingConfigSource({"app": {"modules": []}})]),
    )
    app.state.site = site
    site.provide_capability(ApiCapability, CustomApiCapability())
    register_error_handlers(app)

    @app.get("/api/fail")
    async def fail() -> Response:
        raise RuntimeError("boom")

    with WybraTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/fail")

    assert response.status_code == 500
    assert response.json() == {
        "custom_error": "internal_server_error",
        "custom_message": "Internal Server Error",
    }


def test_api_error_codes_are_status_based_not_heading_based() -> None:
    app = FastAPI()
    site = Site(
        app=app,
        config=ConfigService([MappingConfigSource({"app": {"modules": []}})]),
    )
    app.state.site = site
    site.provide_capability(ApiCapability, CustomApiCapability())
    register_error_handlers(app)

    @app.get("/api/fail")
    async def fail() -> Response:
        raise HTTPException(status_code=418)

    with WybraTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/fail")

    assert response.status_code == 418
    assert response.json()["custom_error"] == "im_a_teapot"


def test_api_error_codes_fallback_for_non_standard_status_codes() -> None:
    presentation = ErrorPresentation(
        status_code=444,
        heading="Request Failed",
        detail="The request could not be completed.",
    )

    assert _api_error_code(presentation) == "http_444"


def test_api_errors_without_api_capability_use_plain_text_fallback() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/fail")
    async def fail() -> Response:
        raise RuntimeError("boom")

    with WybraTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/fail")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("500 Internal Server Error:")


def test_error_classification_uses_htmx_header() -> None:
    app = FastAPI()
    observed = {}
    register_error_handlers(app)

    @app.get("/items")
    async def items(request: Request) -> Response:
        observed["kind"] = _resolve_error_response_kind(request)
        return Response()

    with WybraTestClient(app) as client:
        client.get("/items", headers={"HX-Request": "true"})

    assert observed["kind"] == "partial"


def test_error_classification_uses_accept_header() -> None:
    app = FastAPI()
    observed = {}
    register_error_handlers(app)

    @app.get("/items")
    async def items(request: Request) -> Response:
        observed["kind"] = _resolve_error_response_kind(request)
        return Response()

    with WybraTestClient(app) as client:
        client.get("/items", headers={"Accept": "application/json"})

    assert observed["kind"] == "api"


def test_error_classification_uses_route_type_metadata() -> None:
    from wybra.core.routes import RouteType, route_type

    app = FastAPI()
    observed = {}
    register_error_handlers(app)

    @app.get("/items")
    @route_type(RouteType.API)
    async def items(request: Request) -> Response:
        observed["kind"] = _resolve_error_response_kind(request)
        return Response()

    with WybraTestClient(app) as client:
        client.get("/items")

    assert observed["kind"] == "api"
