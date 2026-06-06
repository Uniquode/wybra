import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Literal, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from jinja2.exceptions import TemplateNotFound
from starlette.exceptions import HTTPException as StarletteHTTPException

from wevra.web.rendering import TemplateRenderer
from wevra.web.routes.contracts import API_PATH_PREFIX, PARTIAL_PATH_PREFIX

RouteSurface = Literal["page", "partial", "api", "static"]

logger = logging.getLogger(__name__)
_SAFE_ERROR_HEADERS: set[str] = {"allow", "www-authenticate", "retry-after"}
ERROR_OPTIONS_STATE_ATTRIBUTE = "wevra_web_error_options"


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
    static_mount_path: str = "/static"
    api_path_prefix: str = API_PATH_PREFIX
    partial_path_prefix: str = PARTIAL_PATH_PREFIX
    page_template: str = "errors/base.html"
    partial_template: str = "errors/fragment.html"


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
    app.add_exception_handler(EmptyBodyResponseException, _handle_empty_body_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_exception)


def _handle_empty_body_error(request: Request, exc: Exception) -> Response:
    empty_body_exc = cast(EmptyBodyResponseException, exc)
    return Response(status_code=empty_body_exc.status_code)


def _handle_http_exception(request: Request, exc: Exception) -> Response:
    http_exc = cast(StarletteHTTPException, exc)
    presentation = _build_error_presentation(
        http_exc.status_code,
        detail=_normalise_http_detail(http_exc.status_code, http_exc.detail),
    )
    return _build_error_response(request, presentation, headers=http_exc.headers)


def _handle_validation_error(request: Request, exc: Exception) -> Response:
    validation_exc = cast(RequestValidationError, exc)
    presentation = _build_error_presentation(422, detail="The request was invalid.")
    surface = _resolve_route_surface(request)
    if surface == "api":
        return JSONResponse(
            status_code=422,
            content=_build_api_error_payload(
                presentation,
                errors=validation_exc.errors(),
            ),
        )

    if surface in ("page", "partial"):
        presentation = replace(
            presentation,
            form_errors=_summarise_validation_errors(validation_exc),
        )

    return _build_error_response(request, presentation)


def _handle_unexpected_exception(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled application error", exc_info=exc)
    presentation = _build_error_presentation(500)
    return _build_error_response(request, presentation)


def _build_error_response(
    request: Request,
    presentation: ErrorPresentation,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    surface = _resolve_route_surface(request)
    if surface == "api":
        response = JSONResponse(
            status_code=presentation.status_code,
            content=_build_api_error_payload(presentation),
        )
        _apply_headers(response, headers)
        return response
    if surface == "static":
        response = PlainTextResponse(
            presentation.heading,
            status_code=presentation.status_code,
        )
        _apply_headers(response, headers)
        return response

    renderer = getattr(request.app.state, "renderer", None)
    if not isinstance(renderer, TemplateRenderer):  # pragma: no cover - defensive
        return _fallback_error_response(surface, presentation, headers=headers)

    context = {
        "page_title": f"{presentation.status_code} {presentation.heading}",
        "heading": presentation.heading,
        "detail": presentation.detail,
        "form_errors": presentation.form_errors,
        "status_code": str(presentation.status_code),
    }

    try:
        if surface == "partial":
            response = renderer.render_partial(
                _error_options(request).partial_template,
                request,
                context,
                status_code=presentation.status_code,
            )
        else:
            response = renderer.render_page(
                _error_options(request).page_template,
                request,
                context,
                status_code=presentation.status_code,
            )
    except TemplateNotFound:
        return _fallback_error_response(surface, presentation, headers=headers)

    _apply_headers(response, headers)
    return response


def _resolve_route_surface(request: Request) -> RouteSurface:
    path = request.url.path
    options = _error_options(request)
    if _matches_path_prefix(path, options.static_mount_path):
        return "static"
    if _matches_path_prefix(path, options.api_path_prefix):
        return "api"
    if _matches_path_prefix(path, options.partial_path_prefix):
        return "partial"
    return "page"


def _error_options(request: Request) -> ErrorHandlerOptions:
    options = getattr(request.app.state, ERROR_OPTIONS_STATE_ATTRIBUTE, None)
    if isinstance(options, ErrorHandlerOptions):
        return options

    return ErrorHandlerOptions()


def _apply_headers(response: Response, headers: Mapping[str, str] | None) -> None:
    if not headers:
        return

    for name, value in headers.items():
        if name.lower() in _SAFE_ERROR_HEADERS:
            response.headers[name] = value


def _fallback_error_response(
    surface: RouteSurface,
    presentation: ErrorPresentation,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    if surface == "api":
        response = JSONResponse(
            status_code=presentation.status_code,
            content=_build_api_error_payload(presentation),
        )
        _apply_headers(response, headers)
        return response

    response = PlainTextResponse(
        f"{presentation.status_code} {presentation.heading}: {presentation.detail}",
        status_code=presentation.status_code,
    )
    _apply_headers(response, headers)
    return response


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
    # Treat a bare reason phrase as “no bespoke detail” so generic fallback copy
    # is used consistently unless a handler supplies something more specific.
    if detail == _reason_phrase(status_code):
        return None

    return detail


def _build_api_error_payload(
    presentation: ErrorPresentation,
    *,
    errors: Sequence[object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error": presentation.heading,
        "detail": presentation.detail,
        "status_code": presentation.status_code,
    }
    if errors is not None:
        payload["errors"] = errors

    return payload


def _matches_path_prefix(path: str, prefix: str) -> bool:
    normalised_prefix = prefix.rstrip("/") or "/"
    return path == normalised_prefix or path.startswith(f"{normalised_prefix}/")


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
