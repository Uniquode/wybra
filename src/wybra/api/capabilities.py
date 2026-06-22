"""Runtime API response capability."""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from http import HTTPStatus
from importlib import import_module
from typing import Protocol, runtime_checkable
from urllib.parse import urlencode

from fastapi import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from wybra.api.config import ApiLinkMode
from wybra.api.settings import ApiSettings
from wybra.core.routes import RouteType
from wybra.core.url_paths import matches_path_prefix
from wybra.site import Site

API_CAPABILITY_MARKER = "provides_api_capability"


@dataclass(frozen=True, slots=True)
class ApiMetadata:
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ApiError:
    code: str
    message: str
    details: object | None = None


@dataclass(frozen=True, slots=True)
class ApiPageLink:
    rel: str
    href: str
    method: str = "GET"


@dataclass(frozen=True, slots=True)
class ApiPaging:
    links: tuple[ApiPageLink, ...] = ()
    cursor: str | None = None
    limit: int | None = None
    has_more: bool | None = None


@runtime_checkable
class ApiCapability(Protocol):
    def is_api_request(
        self,
        request: Request,
        *,
        route_type: RouteType | str | None = None,
    ) -> bool: ...

    def response(
        self,
        data: object,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response: ...

    def paged_response(
        self,
        items: Iterable[object],
        *,
        paging: ApiPaging,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response: ...

    def error_response(
        self,
        error: ApiError,
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
    ) -> Response: ...

    def status_response(
        self,
        *,
        status_code: int,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response: ...

    def validation_error_response(
        self,
        errors: Iterable[object],
        *,
        status_code: int = 422,
        headers: Mapping[str, str] | None = None,
    ) -> Response: ...

    def streaming_response(
        self,
        body: Iterable[bytes] | AsyncIterable[bytes],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> StreamingResponse: ...


@dataclass(frozen=True, slots=True)
class DefaultApiCapability:
    settings: ApiSettings

    def is_api_request(
        self,
        request: Request,
        *,
        route_type: RouteType | str | None = None,
    ) -> bool:
        if route_type is not None and RouteType(route_type) is RouteType.API:
            return True
        return matches_path_prefix(request.url.path, self.settings.path_prefix)

    def response(
        self,
        data: object,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response:
        payload: dict[str, object] = {"data": data}
        if metadata is not None and metadata.values:
            payload["meta"] = dict(metadata.values)
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def paged_response(
        self,
        items: Iterable[object],
        *,
        paging: ApiPaging,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response:
        payload: dict[str, object] = {
            "data": list(items),
            "links": [_link_payload(link) for link in paging.links],
        }
        paging_payload = _paging_payload(paging)
        if paging_payload:
            payload["paging"] = paging_payload
        if metadata is not None and metadata.values:
            payload["meta"] = dict(metadata.values)
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def error_response(
        self,
        error: ApiError,
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        error_payload: dict[str, object] = {
            "code": error.code,
            "message": error.message,
        }
        if error.details is not None:
            error_payload["details"] = error.details
        payload: dict[str, object] = {
            "error": error_payload,
        }
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def status_response(
        self,
        *,
        status_code: int,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
        metadata: ApiMetadata | None = None,
    ) -> Response:
        payload: dict[str, object] = {
            "status": status_code,
            "message": message or _status_phrase(status_code),
        }
        if metadata is not None and metadata.values:
            payload["meta"] = dict(metadata.values)
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def validation_error_response(
        self,
        errors: Iterable[object],
        *,
        status_code: int = 422,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.error_response(
            ApiError(
                code="validation_error",
                message="The request was invalid.",
                details=list(errors),
            ),
            status_code=status_code,
            headers=headers,
        )

    def streaming_response(
        self,
        body: Iterable[bytes] | AsyncIterable[bytes],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> StreamingResponse:
        return StreamingResponse(
            body,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )

    def page_link(
        self,
        request: Request,
        *,
        rel: str,
        cursor: str,
        limit: int,
        mode: ApiLinkMode | None = None,
        method: str = "GET",
    ) -> ApiPageLink:
        link_mode = mode or self.settings.paging_link_mode
        query = urlencode({"cursor": cursor, "limit": str(limit)})
        href = f"?{query}"
        if link_mode is ApiLinkMode.REQUEST_PATH:
            href = f"{request.url.path}{href}"
        return ApiPageLink(rel=rel, href=href, method=method)


async def setup_site(site: Site) -> None:
    settings = ApiSettings.load_settings(site.config)
    site.provide_capability(ApiCapability, DefaultApiCapability(settings))


async def post_setup_site(_site: Site) -> None:
    """Reserved for configured hard API dependency checks."""


def api_provider_configured(modules: tuple[str, ...]) -> bool:
    if "wybra.api" in modules:
        return True
    return any(_module_provides_api_capability(module_name) for module_name in modules)


@cache
def _module_provides_api_capability(module_name: str) -> bool:
    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return False
    return getattr(module, API_CAPABILITY_MARKER, False) is True


def _link_payload(link: ApiPageLink) -> dict[str, str]:
    return {
        "rel": link.rel,
        "href": link.href,
        "method": link.method,
    }


def _paging_payload(paging: ApiPaging) -> dict[str, object]:
    payload: dict[str, object] = {}
    if paging.cursor is not None:
        payload["cursor"] = paging.cursor
    if paging.limit is not None:
        payload["limit"] = paging.limit
    if paging.has_more is not None:
        payload["has_more"] = paging.has_more
    return payload


def _status_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Status"


__all__ = (
    "API_CAPABILITY_MARKER",
    "ApiCapability",
    "ApiError",
    "ApiMetadata",
    "ApiPageLink",
    "ApiPaging",
    "DefaultApiCapability",
    "api_provider_configured",
    "post_setup_site",
    "setup_site",
)
