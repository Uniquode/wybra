import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from wevra.web.forms.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    CsrfProtector,
    csrf_exempt,
    request_form_data,
    validate_csrf,
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


def test_csrf_dependency_allows_safe_methods_on_protected_router() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.get("/form")
    async def form() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).get("/form")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_csrf_dependency_is_noop_when_csrf_protector_missing() -> None:
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.get("/form")
    async def form() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/submit")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    client = TestClient(app)

    get_response = client.get("/form")
    post_response = client.post("/submit", data={"field": "value"})

    assert get_response.status_code == 200
    assert get_response.json() == {"ok": True}
    assert post_response.status_code == 200
    assert post_response.json() == {"ok": True}


def test_csrf_dependency_raises_when_csrf_protector_misconfigured() -> None:
    app = FastAPI()
    app.state.csrf = object()
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/submit")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with pytest.raises(
        RuntimeError, match="CSRF protector is not configured correctly"
    ):
        TestClient(app).post("/submit", data={"field": "value"})


def test_csrf_dependency_rejects_unsafe_methods_without_token() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/form")
    async def submit() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).post("/form", data={"field": "value"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid CSRF token."}


def test_csrf_exempt_allows_route_to_bypass_protected_router() -> None:
    app = FastAPI()
    app.state.csrf = CsrfProtector("test-secret")
    router = APIRouter(dependencies=[Depends(validate_csrf)])

    @router.post("/callback")
    @csrf_exempt
    async def callback() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    response = TestClient(app).post("/callback", data={"field": "value"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
