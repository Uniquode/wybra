import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.forms import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    FormsCapability,
    FormsSettings,
    csrf_exempt,
    request_csrf_response_finalisation,
    request_form_data,
    validate_csrf,
)
from wybra.forms.csrf import CsrfProtector
from wybra.site import start
from wybra.tools.validate import validate_command
from wybra.tools.validation.core import ValidationResult


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
    caplog.set_level(logging.DEBUG, logger="wybra.forms.csrf")

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


def test_forms_settings_generates_local_secret(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wybra.forms.settings")

    settings = FormsSettings()

    assert settings.token_secret
    assert settings.cookie_secure is False
    assert "Generated startup-local CSRF token secret." in caplog.text


def test_forms_settings_load_settings_uses_config_service_sources() -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "deployment_environment": "production",
                        "modules": ("wybra.forms",),
                    },
                    "wybra.forms": {
                        "csrf_token_secret": "production-csrf-secret",
                        "csrf_cookie_secure": "true",
                    },
                }
            )
        ],
    )

    settings = FormsSettings.load_settings(config)

    assert settings.deployment_environment == "production"
    assert settings.token_secret == "production-csrf-secret"
    assert settings.cookie_secure is True


def test_forms_settings_load_settings_rejects_blank_token_secret() -> None:
    with pytest.raises(ConfigSourceError, match="csrf_token_secret"):
        ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {"modules": ("wybra.forms",)},
                        "wybra.forms": {"csrf_token_secret": "   "},
                    }
                )
            ],
        )


@pytest.mark.anyio
async def test_forms_setup_provides_forms_capability(tmp_path) -> None:
    app = FastAPI()
    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.forms",),
                },
            }
        ),
    )

    assert site.require_capability(FormsCapability)
    assert isinstance(app.state.csrf, CsrfProtector)


@pytest.mark.anyio
async def test_forms_setup_finalises_csrf_cookie_when_requested(tmp_path) -> None:
    app = FastAPI()

    @app.get("/form")
    async def form(request: Request) -> PlainTextResponse:
        request_csrf_response_finalisation(request)
        return PlainTextResponse("ok")

    @app.get("/partials/form")
    async def partial_form() -> PlainTextResponse:
        return PlainTextResponse("ok")

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.forms",),
                },
            }
        ),
    )

    with TestClient(app) as client:
        response = client.get("/form")
        partial_response = client.get("/partials/form")

    assert response.status_code == 200
    assert CSRF_COOKIE_NAME in response.cookies
    assert partial_response.status_code == 200
    assert CSRF_COOKIE_NAME not in partial_response.cookies


@pytest.mark.anyio
async def test_web_setup_omits_forms_behaviour_without_forms_module(tmp_path) -> None:
    app = FastAPI()
    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.web",),
                },
                "app.routes": {"prefixes": {"wybra.web": {}}},
            }
        ),
    )

    assert not hasattr(app.state, "csrf")


def test_validate_forms_target_is_available(monkeypatch, tmp_path) -> None:
    class Settings:
        modules = ("wybra.forms",)
        config = ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {
                            "config_path": tmp_path / "app.toml",
                            "project_root": tmp_path,
                            "modules": ("wybra.forms",),
                        },
                    }
                )
            ],
        )

    monkeypatch.setattr(
        "wybra.tools.validate._build_settings",
        lambda _overrides: Settings(),
    )

    assert validate_command.main(args=["forms"], standalone_mode=False) == 0


def test_validate_forms_reports_loaded_settings() -> None:
    from wybra.forms.validation import validate_forms

    result = validate_forms(
        type(
            "Settings",
            (),
            {
                "modules": ("wybra.forms",),
                "config": ConfigService(
                    [
                        MappingConfigSource(
                            {
                                "app": {"modules": ("wybra.forms",)},
                            }
                        )
                    ],
                ),
            },
        )()
    )

    assert isinstance(result, ValidationResult)
    assert result.is_ok
    assert any(
        check.description.startswith("forms settings load") for check in result.checks
    )
