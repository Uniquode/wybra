from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from wybra.site import SiteCapabilityError
from wybra.views import (
    APIResult,
    APIView,
    HTMLView,
    Page,
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
    response = await View().dispatch(_request("DELETE"))

    assert response.status_code == 405


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
        def render_page(
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

        def render_partial(
            self,
            request: Request,
            template_name: str,
            context: dict[str, Any],
            *,
            status_code: int = 200,
        ) -> HTMLResponse:
            return self.render_page(
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
                page=Page(number=1, size=25, total=1),
            )

    response = await ExampleAPIView().dispatch(_request())

    assert isinstance(response, JSONResponse)
    assert response.body == (
        b'{"data":[{"name":"Ada"}],"page":{"number":1,"size":25,"total":1}}'
    )


@pytest.mark.anyio
async def test_api_view_can_delegate_final_formatting() -> None:
    class Formatter:
        def response_from_result(self, result: object) -> Response:
            return JSONResponse({"wrapped": result})

    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    response = await ExampleAPIView(formatter=Formatter()).dispatch(_request())

    assert response.body == b'{"wrapped":{"ok":"yes"}}'
