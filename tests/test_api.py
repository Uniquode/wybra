from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request
from starlette.responses import Response, StreamingResponse

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.routes import RouteType


def _config(values: dict[str, object] | None = None) -> ConfigService:
    return ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {"modules": ["wybra.api"]},
                    "app.api": values or {},
                }
            )
        ]
    )


def _request(path: str, *, query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": query_string,
        }
    )


def _json(response: Response) -> object:
    return json.loads(response.body)


def test_api_settings_load_from_module_config() -> None:
    from wybra.api import ApiLinkMode, ApiSettings

    settings = ApiSettings.load_settings(
        _config({"path_prefix": "service", "paging_link_mode": "request_path"})
    )

    assert settings.path_prefix == "/service"
    assert settings.paging_link_mode == ApiLinkMode.REQUEST_PATH


def test_default_api_capability_classifies_api_requests() -> None:
    from wybra.api import ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))

    assert capability.is_api_request(_request("/api/widgets"))
    assert capability.is_api_request(_request("/internal"), route_type=RouteType.API)
    assert not capability.is_api_request(_request("/pages/home"))


def test_default_api_capability_builds_success_response() -> None:
    from wybra.api import ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.response({"name": "Ada"}, status_code=201)

    assert response.status_code == 201
    assert _json(response) == {"data": {"name": "Ada"}}


def test_default_api_capability_builds_error_response() -> None:
    from wybra.api import ApiError, ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.error_response(
        ApiError(code="not_found", message="Item was not found."),
        status_code=404,
        headers={"Allow": "GET"},
    )

    assert response.status_code == 404
    assert response.headers["allow"] == "GET"
    assert _json(response) == {
        "error": {
            "code": "not_found",
            "message": "Item was not found.",
        }
    }


def test_default_api_capability_builds_status_response() -> None:
    from wybra.api import ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.status_response(status_code=202)

    assert response.status_code == 202
    assert _json(response) == {"status": 202, "message": "Accepted"}


def test_default_api_capability_builds_validation_error_response() -> None:
    from wybra.api import ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.validation_error_response(
        [{"loc": ("body", "name"), "msg": "Missing"}],
    )

    assert response.status_code == 422
    assert _json(response) == {
        "error": {
            "code": "validation_error",
            "message": "The request was invalid.",
            "details": [{"loc": ["body", "name"], "msg": "Missing"}],
        }
    }


def test_paged_response_uses_pathless_hateoas_links_by_default() -> None:
    from wybra.api import ApiPageLink, ApiPaging, ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.paged_response(
        [{"name": "Ada"}],
        paging=ApiPaging(
            links=(ApiPageLink("next", "?cursor=def&limit=25"),),
            cursor="abc",
            limit=25,
            has_more=True,
        ),
    )

    assert _json(response) == {
        "data": [{"name": "Ada"}],
        "links": [{"rel": "next", "href": "?cursor=def&limit=25", "method": "GET"}],
        "paging": {"cursor": "abc", "limit": 25, "has_more": True},
    }


def test_page_link_can_include_request_path() -> None:
    from wybra.api import ApiLinkMode, ApiPageLink, ApiSettings, DefaultApiCapability

    capability = DefaultApiCapability(
        ApiSettings.load_settings(_config({"paging_link_mode": "request_path"}))
    )
    link = capability.page_link(
        _request("/api/items", query_string=b"cursor=abc&limit=25"),
        rel="self",
        cursor="abc",
        limit=25,
        mode=ApiLinkMode.REQUEST_PATH,
    )

    assert isinstance(link, ApiPageLink)
    assert link.href == "/api/items?cursor=abc&limit=25"


@pytest.mark.anyio
async def test_streaming_response_delegates_through_api_capability() -> None:
    from wybra.api import ApiSettings, DefaultApiCapability

    async def stream() -> AsyncIterator[bytes]:
        yield b"one\n"
        yield b"two\n"

    capability = DefaultApiCapability(ApiSettings.load_settings(_config()))
    response = capability.streaming_response(
        stream(), media_type="application/x-ndjson"
    )

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "application/x-ndjson"


def test_replacement_provider_can_satisfy_api_capability_protocol() -> None:
    from wybra.api import ApiCapability

    class ReplacementApi:
        def is_api_request(self, request: Request, *, route_type=None) -> bool:
            return request.url.path.startswith("/service")

        def response(self, data, *, status_code=200, headers=None, metadata=None):
            return Response(f"custom:{data}", status_code=status_code, headers=headers)

        def paged_response(
            self,
            items,
            *,
            paging,
            status_code=200,
            headers=None,
            metadata=None,
        ):
            return Response(f"custom:{len(tuple(items))}", status_code=status_code)

        def error_response(self, error, *, status_code, headers=None):
            return Response(error.message, status_code=status_code, headers=headers)

        def status_response(
            self,
            *,
            status_code,
            message=None,
            headers=None,
            metadata=None,
        ):
            return Response(message or "status", status_code=status_code)

        def validation_error_response(
            self,
            errors,
            *,
            status_code=422,
            headers=None,
        ):
            return Response(f"custom:{len(tuple(errors))}", status_code=status_code)

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

    assert isinstance(ReplacementApi(), ApiCapability)


@pytest.mark.anyio
async def test_api_module_setup_provides_api_capability() -> None:
    from wybra.api import ApiCapability
    from wybra.site import start

    app = FastAPI()
    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {"modules": ["wybra.api"]},
            }
        ),
    )

    assert site.has_capability(ApiCapability)
