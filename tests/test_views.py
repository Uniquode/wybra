from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from wybra.api import ApiCapability, ApiPaging, ApiSettings, DefaultApiCapability
from wybra.site import SiteCapabilityError
from wybra.views import (
    APIResult,
    APIView,
    HTMLView,
    TemplateView,
    View,
)


def _request(method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
        }
    )


@pytest.mark.anyio
async def test_view_dispatches_to_matching_http_method() -> None:
    class ExampleView(View):
        async def get(self, request: Request, *, item_id: str) -> dict[str, str]:
            return {"method": request.method, "item_id": item_id}

    response = await ExampleView().dispatch(_request(), item_id="42")

    assert isinstance(response, JSONResponse)
    assert response.body == b'{"method":"GET","item_id":"42"}'


@pytest.mark.anyio
async def test_view_returns_method_not_allowed_for_missing_handler() -> None:
    class ExampleView(View):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

        def post(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    response = await ExampleView().dispatch(_request("DELETE"))

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, POST"


def test_view_model_is_available_to_subclass() -> None:
    @dataclass(frozen=True, slots=True)
    class SomeDataModel:
        name: str

    class ModelView(View):
        model = SomeDataModel

    assert ModelView.model is SomeDataModel


@pytest.mark.anyio
async def test_html_view_renders_literal_html_without_template_capability() -> None:
    class ExampleHTMLView(HTMLView):
        def get(self, _request: Request) -> str:
            return "<main>hello</main>"

    response = await ExampleHTMLView().dispatch(_request())

    assert isinstance(response, HTMLResponse)
    assert response.body == b"<main>hello</main>"


@pytest.mark.anyio
async def test_template_view_renders_through_template_capability() -> None:
    class FakeTemplateCapability:
        async def render_template(
            self, template_name: str, context: dict[str, Any]
        ) -> str:
            return f"{template_name}:{context['name']}"

        async def render_page(
            self,
            request: Request,
            template_name: str,
            context: dict[str, Any],
            *,
            status_code: int = 200,
        ) -> HTMLResponse:
            return HTMLResponse(
                f"{request.method}:{template_name}:{context['name']}",
                status_code=status_code,
            )

        async def render_partial(
            self,
            request: Request,
            template_name: str,
            context: dict[str, Any],
            *,
            status_code: int = 200,
        ) -> HTMLResponse:
            return await self.render_page(
                request,
                template_name,
                context,
                status_code=status_code,
            )

    view = TemplateView(
        "pages/home.html",
        context_builder=lambda _request: {"name": "home"},
    )

    response = await view.render(_request(), FakeTemplateCapability())

    assert response.body == b"GET:pages/home.html:home"


@pytest.mark.anyio
async def test_template_view_reports_missing_template_capability() -> None:
    view = TemplateView("pages/home.html")

    with pytest.raises(SiteCapabilityError, match="TemplateCapability"):
        await view.render(_request(), None)


@pytest.mark.anyio
async def test_api_view_renders_paged_api_result() -> None:
    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> APIResult:
            return APIResult(
                data=[{"name": "Ada"}],
                paging=ApiPaging(cursor="abc", limit=25, has_more=False),
            )

    response = await ExampleAPIView(api=DefaultApiCapability(ApiSettings())).dispatch(
        _request()
    )

    assert isinstance(response, JSONResponse)
    assert response.body == (
        b'{"data":[{"name":"Ada"}],"links":[],"paging":{"cursor":"abc",'
        b'"limit":25,"has_more":false}}'
    )


@pytest.mark.anyio
async def test_api_view_can_delegate_final_formatting() -> None:
    class ApiFormatter(ApiCapability):
        def is_api_request(self, request: Request, *, route_type=None) -> bool:
            return True

        def response(self, data, *, status_code=200, headers=None, metadata=None):
            return JSONResponse({"wrapped": data}, status_code=status_code)

        def paged_response(
            self,
            items,
            *,
            paging,
            status_code=200,
            headers=None,
            metadata=None,
        ):
            return JSONResponse(
                {"wrapped": list(items), "links": len(paging.links)},
                status_code=status_code,
            )

        def error_response(self, error, *, status_code, headers=None):
            return JSONResponse({"error": error.message}, status_code=status_code)

        def status_response(
            self,
            *,
            status_code,
            message=None,
            headers=None,
            metadata=None,
        ):
            return JSONResponse({"status": message}, status_code=status_code)

        def validation_error_response(
            self,
            errors,
            *,
            status_code=422,
            headers=None,
        ):
            return JSONResponse({"errors": list(errors)}, status_code=status_code)

        def streaming_response(
            self,
            body,
            *,
            status_code=200,
            headers=None,
            media_type=None,
        ):
            return Response(status_code=status_code, headers=headers)

    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    api: ApiCapability = ApiFormatter()
    response = await ExampleAPIView(api=api).dispatch(_request())

    assert response.body == b'{"wrapped":{"ok":"yes"}}'


@pytest.mark.anyio
async def test_api_view_reports_missing_api_capability() -> None:
    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    with pytest.raises(SiteCapabilityError, match="configure wybra.api"):
        await ExampleAPIView().dispatch(_request())
