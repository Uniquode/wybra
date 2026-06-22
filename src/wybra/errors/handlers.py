import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Literal, Protocol, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, Response
from jinja2.exceptions import TemplateNotFound
from starlette.exceptions import HTTPException as StarletteHTTPException

from wybra.api import ApiCapability, ApiError
from wybra.core.routes import ROUTE_TYPE_ATTRIBUTE, RouteType
from wybra.core.routes.contracts import PARTIAL_PATH_PREFIX
from wybra.core.url_paths import matches_path_prefix
from wybra.errors.mappings import ErrorMapping, translate_exception
from wybra.site import SiteCapabilityError, get_site
from wybra.template.rendering import template_capability_from

ErrorResponseKind = Literal["page", "partial", "api", "static"]
StaticMountPathResolver = Callable[[], str | None]

logger = logging.getLogger(__name__)
_SAFE_ERROR_HEADERS: set[str] = {"allow", "www-authenticate", "retry-after"}
ERROR_OPTIONS_STATE_ATTRIBUTE = "wybra_errors_options"


class _ErrorHandlingCapability(Protocol):
    def response_for_exception(self, request: Request, exc: Exception) -> Response: ...


@dataclass(frozen=True, slots=True)
class EmptyBodyResponseException(Exception):
    status_code: int


@dataclass(frozen=True, slots=True)
class ErrorPresentation:
    status_code: int
    heading: str
    detail: str
    form_errors: dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True, slots=True)
class ErrorHandlerOptions:
    static_mount_path: str | StaticMountPathResolver | None = None
    partial_path_prefix: str = PARTIAL_PATH_PREFIX
    page_template: str = "errors/base.html"
    partial_template: str = "errors/fragment.html"
    mappings: tuple[ErrorMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class DefaultErrorHandlingCapability:
    """Default exception-to-response translator."""

    def response_for_exception(self, request: Request, exc: Exception) -> Response:
        if isinstance(exc, EmptyBodyResponseException):
            return Response(status_code=exc.status_code)
        if isinstance(exc, StarletteHTTPException):
            return _response_for_http_exception(request, exc)
        if isinstance(exc, RequestValidationError):
            return _response_for_validation_error(request, exc)
        return _response_for_unexpected_exception(request, exc)


def register_error_handlers(
    app: FastAPI,
    *,
    options: ErrorHandlerOptions | None = None,
) -> None:
    setattr(
        app.state,
        ERROR_OPTIONS_STATE_ATTRIBUTE,
        options or ErrorHandlerOptions(),
    )
    app.add_exception_handler(EmptyBodyResponseException, _handle_exception)
    app.add_exception_handler(StarletteHTTPException, _handle_exception)
    app.add_exception_handler(RequestValidationError, _handle_exception)
    app.add_exception_handler(Exception, _handle_exception)


def _handle_exception(request: Request, exc: Exception) -> Response:
    translated = translate_exception(exc, mappings=_error_options(request).mappings)
    if not isinstance(translated, Exception):
        translated = exc
    return _error_capability(request).response_for_exception(request, translated)


def _handle_http_exception(request: Request, exc: Exception) -> Response:
    http_exc = cast(StarletteHTTPException, exc)
    return _response_for_http_exception(request, http_exc)


def _response_for_http_exception(
    request: Request,
    http_exc: StarletteHTTPException,
) -> Response:
    presentation = _build_error_presentation(
        http_exc.status_code,
        detail=_normalise_http_detail(http_exc.status_code, http_exc.detail),
    )
    return _build_error_response(request, presentation, headers=http_exc.headers)


def _handle_validation_error(request: Request, exc: Exception) -> Response:
    validation_exc = cast(RequestValidationError, exc)
    return _response_for_validation_error(request, validation_exc)


def _response_for_validation_error(
    request: Request,
    validation_exc: RequestValidationError,
) -> Response:
    presentation = _build_error_presentation(422, detail="The request was invalid.")
    response_kind = _resolve_error_response_kind(request)
    if response_kind == "api":
        return _build_api_validation_error_response(
            request,
            presentation,
            errors=validation_exc.errors(),
        )

    if response_kind in ("page", "partial"):
        presentation = replace(
            presentation,
            form_errors=_summarise_validation_errors(validation_exc),
        )

    return _build_error_response(request, presentation)


def _handle_unexpected_exception(request: Request, exc: Exception) -> Response:
    return _response_for_unexpected_exception(request, exc)


def _response_for_unexpected_exception(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled application error", exc_info=exc)
    presentation = _build_error_presentation(500)
    return _build_error_response(request, presentation)


def _build_error_response(
    request: Request,
    presentation: ErrorPresentation,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response_kind = _resolve_error_response_kind(request)
    if response_kind == "api":
        return _build_api_error_response(
            request,
            presentation,
            headers=headers,
        )
    if response_kind == "static":
        response = PlainTextResponse(
            presentation.heading,
            status_code=presentation.status_code,
        )
        _apply_headers(response, headers)
        return response

    try:
        templates = template_capability_from(request)
    except SiteCapabilityError:
        return _fallback_error_response(response_kind, presentation, headers=headers)

    context = {
        "page_title": f"{presentation.status_code} {presentation.heading}",
        "heading": presentation.heading,
        "detail": presentation.detail,
        "form_errors": presentation.form_errors,
        "status_code": str(presentation.status_code),
    }

    try:
        if response_kind == "partial":
            response = templates.render_partial(
                request,
                _error_options(request).partial_template,
                context,
                status_code=presentation.status_code,
            )
        else:
            response = templates.render_page(
                request,
                _error_options(request).page_template,
                context,
                status_code=presentation.status_code,
            )
    except TemplateNotFound:
        return _fallback_error_response(response_kind, presentation, headers=headers)

    _apply_headers(response, headers)
    return response


def _resolve_error_response_kind(request: Request) -> ErrorResponseKind:
    path = request.url.path
    route_kind = _route_response_kind(request)
    if route_kind is not None:
        return route_kind
    api = _optional_api_capability(request)
    if api is not None and api.is_api_request(request):
        return "api"
    if _accepts_json(request):
        return "api"
    if _is_htmx_request(request):
        return "partial"
    options = _error_options(request)
    static_mount_path = _resolve_static_mount_path(options)
    if static_mount_path is not None and _matches_path_prefix(path, static_mount_path):
        return "static"
    if _matches_path_prefix(path, options.partial_path_prefix):
        return "partial"
    return "page"


def _route_response_kind(request: Request) -> ErrorResponseKind | None:
    endpoint = request.scope.get("endpoint")
    route_type = getattr(endpoint, ROUTE_TYPE_ATTRIBUTE, None)
    if route_type == RouteType.API.value:
        return "api"
    if route_type == RouteType.PARTIAL.value:
        return "partial"
    if route_type == RouteType.PAGE.value:
        return "page"
    if route_type == RouteType.STATIC.value:
        return "static"
    return None


def _accepts_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if not accept:
        return False
    accepted = {item.split(";", 1)[0].strip().lower() for item in accept.split(",")}
    return "application/json" in accepted or "application/*" in accepted


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _error_options(request: Request) -> ErrorHandlerOptions:
    options = getattr(request.app.state, ERROR_OPTIONS_STATE_ATTRIBUTE, None)
    if isinstance(options, ErrorHandlerOptions):
        return options

    return ErrorHandlerOptions()


def _resolve_static_mount_path(options: ErrorHandlerOptions) -> str | None:
    static_mount_path = options.static_mount_path
    if isinstance(static_mount_path, str) or static_mount_path is None:
        return static_mount_path
    return static_mount_path()


def _apply_headers(response: Response, headers: Mapping[str, str] | None) -> None:
    if not headers:
        return

    for name, value in headers.items():
        if name.lower() in _SAFE_ERROR_HEADERS:
            response.headers[name] = value


def _fallback_error_response(
    response_kind: ErrorResponseKind,
    presentation: ErrorPresentation,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    if response_kind == "api":
        return _build_api_error_response(
            None,
            presentation,
            headers=headers,
        )

    return _plain_text_error_response(presentation, headers=headers)


def _build_error_presentation(
    status_code: int, *, detail: str | None = None
) -> ErrorPresentation:
    heading = _reason_phrase(status_code)
    fallback_detail = _default_detail(status_code, heading)
    return ErrorPresentation(
        status_code=status_code,
        heading=heading,
        detail=detail if isinstance(detail, str) and detail else fallback_detail,
    )


def _normalise_http_detail(status_code: int, detail: str | None) -> str | None:
    if not isinstance(detail, str) or not detail:
        return None
    if detail == _reason_phrase(status_code):
        return None

    return detail


def _build_api_error_response(
    request: Request | None,
    presentation: ErrorPresentation,
    *,
    details: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    api = _optional_api_capability(request) if request is not None else None
    if api is not None:
        return api.error_response(
            ApiError(
                code=_api_error_code(presentation),
                message=presentation.heading,
                details=details,
            ),
            status_code=presentation.status_code,
            headers=_safe_headers(headers),
        )

    return _plain_text_error_response(presentation, headers=headers)


def _build_api_validation_error_response(
    request: Request,
    presentation: ErrorPresentation,
    *,
    errors: object,
) -> Response:
    api = _optional_api_capability(request)
    if api is not None and isinstance(errors, list):
        return api.validation_error_response(
            errors,
            status_code=presentation.status_code,
        )
    return _plain_text_error_response(presentation)


def _plain_text_error_response(
    presentation: ErrorPresentation,
    *,
    headers: Mapping[str, str] | None = None,
) -> PlainTextResponse:
    response = PlainTextResponse(
        f"{presentation.status_code} {presentation.heading}: {presentation.detail}",
        status_code=presentation.status_code,
    )
    _apply_headers(response, headers)
    return response


def _optional_api_capability(request: Request | None) -> ApiCapability | None:
    if request is None:
        return None
    try:
        return get_site(request.app).optional_capability(ApiCapability)
    except SiteCapabilityError:
        return None


def _error_capability(request: Request) -> _ErrorHandlingCapability:
    try:
        from wybra.errors.capabilities import ErrorHandlingCapability

        capability = get_site(request.app).optional_capability(ErrorHandlingCapability)
    except SiteCapabilityError:
        return DefaultErrorHandlingCapability()
    if capability is None:
        return DefaultErrorHandlingCapability()
    return capability


def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in _SAFE_ERROR_HEADERS
    }


def _api_error_code(presentation: ErrorPresentation) -> str:
    try:
        return HTTPStatus(presentation.status_code).name.lower()
    except ValueError:
        return f"http_{presentation.status_code}"


def _matches_path_prefix(path: str, prefix: str) -> bool:
    return matches_path_prefix(path, prefix)


def _summarise_validation_errors(
    exc: RequestValidationError,
) -> dict[str, tuple[str, ...]] | None:
    field_errors: dict[str, list[str]] = {}
    for error in exc.errors():
        raw_location = error.get("loc")
        if isinstance(raw_location, tuple | list):
            location = tuple(raw_location)
        else:
            location = ()
        display_field: str | None = None
        if len(location) >= 2 and isinstance(location[0], str):
            display_field = str(location[1])
        elif location:
            display_field = str(location[-1])

        if not display_field:
            continue

        message = str(error.get("msg") or "Invalid value")
        field_errors.setdefault(display_field, []).append(message)

    if not field_errors:
        return None

    return {
        field_name: tuple(messages) for field_name, messages in field_errors.items()
    }


def _reason_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request Failed"


def _default_detail(status_code: int, heading: str) -> str:
    return _DEFAULT_DETAILS.get(
        status_code, f"The request could not be completed ({status_code} {heading})."
    )


_DEFAULT_DETAILS: dict[int, str] = {
    400: "The request could not be understood.",
    401: "Authentication is required to access this resource.",
    403: "You do not have permission to access this resource.",
    404: "The requested resource could not be found.",
    405: "The request method is not allowed for this resource.",
    409: "The request could not be completed because of a conflict.",
    422: "The request was invalid.",
    429: "Too many requests were made in a short period.",
    500: "An internal server error prevented the request from completing.",
}
