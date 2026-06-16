import base64
import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import FormData

from wybra.web.forms.security import is_form_content_type, is_safe_method

CSRF_COOKIE_NAME = "wybra_web_csrf"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_DATA_STATE_ATTR = "csrf_form_data"
CSRF_MAX_FORM_BODY_BYTES = 1_048_576
CSRF_NONCE_MAX_LENGTH = 256
CSRF_NONCE_MIN_LENGTH = 32
CSRF_TOKEN_BYTES = 32
CSRF_TOKEN_SEPARATOR = "."
CSRF_EXEMPT_ENDPOINT_ATTR = "__wybra_csrf_exempt__"

logger = logging.getLogger(__name__)


async def request_form_data(request: Request) -> FormData:
    form_data = getattr(request.state, CSRF_FORM_DATA_STATE_ATTR, None)
    if isinstance(form_data, FormData):
        return form_data

    form_data = await request.form()
    setattr(request.state, CSRF_FORM_DATA_STATE_ATTR, form_data)
    return form_data


def csrf_exempt(func: Callable[..., Any]) -> Callable[..., Any]:
    setattr(func, CSRF_EXEMPT_ENDPOINT_ATTR, True)
    return func


async def validate_csrf(request: Request) -> None:
    endpoint = request.scope.get("endpoint")
    if getattr(endpoint, CSRF_EXEMPT_ENDPOINT_ATTR, False):
        return

    protector = getattr(request.app.state, "csrf", None)
    if protector is None:
        return
    if not isinstance(protector, CsrfProtector):  # pragma: no cover - defensive
        raise RuntimeError("CSRF protector is not configured correctly.")

    if not await protector.validate_request(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


@dataclass(frozen=True, slots=True)
class CsrfProtector:
    secret: str
    cookie_name: str = CSRF_COOKIE_NAME
    field_name: str = CSRF_FIELD_NAME
    cookie_secure: bool = False
    max_form_body_bytes: int = CSRF_MAX_FORM_BODY_BYTES

    def token_context(self, request: Request) -> dict[str, str]:
        return {
            "csrf_field_name": self.field_name,
            "csrf_header_name": CSRF_HEADER_NAME,
            "csrf_token": self.create_token(self._request_nonce(request)),
        }

    def set_cookie(self, request: Request, response: Response) -> None:
        response.set_cookie(
            self.cookie_name,
            self._request_nonce(request),
            path="/",
            secure=self.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    async def validate_request(self, request: Request) -> bool:
        if is_safe_method(request.method):
            return True

        cookie_nonce = request.cookies.get(self.cookie_name)
        submitted_token = request.headers.get(CSRF_HEADER_NAME)
        if submitted_token is not None:
            if not cookie_nonce:
                return self._reject(request, "missing_cookie_nonce_for_header")
            if self.validate_token(submitted_token, cookie_nonce):
                return True

            return self._reject(request, "invalid_header_token")

        if not self._can_parse_form_body(request):
            return False

        try:
            form_data = await request.form()
        except Exception:
            return self._reject(request, "form_parse_failed")
        setattr(request.state, CSRF_FORM_DATA_STATE_ATTR, form_data)
        submitted_token = form_data.get(self.field_name)
        if not isinstance(submitted_token, str) or not cookie_nonce:
            return self._reject(request, "missing_form_token_or_cookie_nonce")

        if self.validate_token(submitted_token, cookie_nonce):
            return True

        return self._reject(request, "invalid_form_token")

    def create_token(self, nonce: str) -> str:
        return f"{nonce}{CSRF_TOKEN_SEPARATOR}{self._signature(nonce)}"

    def validate_token(self, token: str, cookie_nonce: str) -> bool:
        nonce, separator, signature = token.partition(CSRF_TOKEN_SEPARATOR)
        if separator != CSRF_TOKEN_SEPARATOR or nonce != cookie_nonce:
            return False

        expected_signature = self._signature(nonce)
        return hmac.compare_digest(signature, expected_signature)

    def _request_nonce(self, request: Request) -> str:
        existing_nonce = getattr(request.state, "csrf_nonce", None)
        if isinstance(existing_nonce, str):
            return existing_nonce

        cookie_nonce = request.cookies.get(self.cookie_name)
        nonce = (
            cookie_nonce
            if isinstance(cookie_nonce, str) and self._is_valid_nonce(cookie_nonce)
            else token_urlsafe(CSRF_TOKEN_BYTES)
        )
        request.state.csrf_nonce = nonce
        return nonce

    def _signature(self, nonce: str) -> str:
        digest = hmac.new(
            self.secret.encode("utf-8"),
            nonce.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @staticmethod
    def _is_valid_nonce(nonce: str) -> bool:
        return (
            CSRF_NONCE_MIN_LENGTH <= len(nonce) <= CSRF_NONCE_MAX_LENGTH
            and CSRF_TOKEN_SEPARATOR not in nonce
        )

    def _can_parse_form_body(self, request: Request) -> bool:
        if not is_form_content_type(request.headers.get("content-type", "")):
            return self._reject(request, "unsupported_content_type")

        content_length = request.headers.get("content-length")
        if content_length is None:
            return self._reject(request, "missing_content_length")

        try:
            body_size = int(content_length)
        except ValueError:
            return self._reject(request, "invalid_content_length")

        if body_size < 0:
            return self._reject(request, "invalid_content_length")

        if body_size > self.max_form_body_bytes:
            return self._reject(request, "form_body_too_large")

        return True

    def _reject(self, request: Request, reason: str) -> bool:
        logger.debug(
            "CSRF request rejected.",
            extra={
                "csrf_reason": reason,
                "method": request.method,
                "path": request.scope.get("path", ""),
            },
        )
        return False
