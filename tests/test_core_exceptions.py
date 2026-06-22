from __future__ import annotations

from starlette.exceptions import HTTPException

from wybra.core.exceptions import Http403, Http404, HttpException


def test_http_exception_defaults_to_internal_server_error() -> None:
    exc = HttpException()

    assert isinstance(exc, HTTPException)
    assert exc.status_code == 500
    assert exc.detail == "Internal Server Error"


def test_http_exception_accepts_status_detail_and_headers() -> None:
    exc = HttpException(
        "Request was invalid.",
        status_code=400,
        headers={"Retry-After": "30"},
    )

    assert exc.status_code == 400
    assert exc.detail == "Request was invalid."
    assert exc.headers == {"Retry-After": "30"}


def test_http_exception_preserves_explicit_empty_detail() -> None:
    exc = HttpException("")

    assert exc.detail == ""


def test_http_convenience_exceptions_use_status_defaults() -> None:
    assert Http404().status_code == 404
    assert Http404().detail == "Not Found"
    assert Http403().status_code == 403
    assert Http403().detail == "Forbidden"


def test_http_exception_preserves_python_exception_chaining() -> None:
    cause = LookupError("missing")

    try:
        raise Http404("Page not found.") from cause
    except Http404 as exc:
        chained = exc

    assert chained.__cause__ is cause
