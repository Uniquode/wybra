import asyncio
import json
import logging
from typing import Any

from fastapi import Request

from wevra.web.forms.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    CsrfProtector,
    request_form_data,
)


def csrf_request(
    *,
    method: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [
                (name.lower().encode("ascii"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
        },
        receive,
    )


def test_csrf_form_validation_rejects_non_form_content_type() -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret")
    token = protector.create_token(nonce)
    body = json.dumps({CSRF_FIELD_NAME: token}).encode("utf-8")
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/json",
            "content-length": str(len(body)),
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )

    assert asyncio.run(protector.validate_request(request)) is False


def test_csrf_form_validation_rejects_oversized_form_body() -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret", max_form_body_bytes=8)
    token = protector.create_token(nonce)
    body = f"{CSRF_FIELD_NAME}={token}".encode()
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(len(body)),
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )

    assert asyncio.run(protector.validate_request(request)) is False


def test_csrf_form_validation_caches_parsed_form_for_downstream_views() -> None:
    async def assert_form_cache() -> None:
        nonce = "a" * 32
        protector = CsrfProtector("test-secret")
        token = protector.create_token(nonce)
        body = f"{CSRF_FIELD_NAME}={token}&field=value".encode()
        request = csrf_request(
            method="POST",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "content-length": str(len(body)),
                "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
            },
            body=body,
        )

        assert await protector.validate_request(request) is True
        form_data = await request_form_data(request)
        assert form_data.get(CSRF_FIELD_NAME) == token
        assert form_data.get("field") == "value"

    asyncio.run(assert_form_cache())


def test_csrf_form_validation_logs_rejection_reason(caplog) -> None:
    nonce = "a" * 32
    protector = CsrfProtector("test-secret")
    token = protector.create_token(nonce)
    body = f"{CSRF_FIELD_NAME}={token}".encode()
    request = csrf_request(
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "cookie": f"{CSRF_COOKIE_NAME}={nonce}",
        },
        body=body,
    )
    caplog.set_level(logging.DEBUG, logger="wevra.web.forms.csrf")

    assert asyncio.run(protector.validate_request(request)) is False
    assert "CSRF request rejected." in caplog.text
    assert any(
        getattr(record, "csrf_reason", None) == "missing_content_length"
        for record in caplog.records
    )
