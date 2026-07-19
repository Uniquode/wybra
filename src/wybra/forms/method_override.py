"""Safe native-form HTTP method override before FastAPI route matching."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wybra.forms.security import FORM_BODY_MAX_BYTES, normalise_content_type

_ALLOWED_METHODS = frozenset({"PATCH", "PUT", "DELETE"})
_SUPPORTED_CONTENT_TYPES = frozenset(
    {"application/json", "application/x-www-form-urlencoded", "multipart/form-data"}
)


class MethodOverrideMiddleware:
    """Translate a narrowly allowed body `_method` in POST requests only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        content_type = normalise_content_type(headers.get("content-type", ""))
        if scope["method"] != "POST" or content_type not in _SUPPORTED_CONTENT_TYPES:
            await self.app(scope, receive, send)
            return
        if not _has_small_content_length(headers):
            await self.app(scope, receive, send)
            return
        body = await Request(scope, receive).body()
        request = Request(scope, _replay_receive(body))
        if content_type == "application/json":
            try:
                override = _json_method_override(await request.json())
            except ValueError:
                await self.app(scope, _replay_receive(body), send)
                return
        else:
            try:
                override = _form_method_override_from_form(await request.form())
            except Exception:
                await self.app(scope, _replay_receive(body), send)
                return
        if override is not None:
            scope = {**scope, "method": override}
        await self.app(scope, _replay_receive(body), send)


def _has_small_content_length(headers: Headers) -> bool:
    content_length = headers.get("content-length")
    if content_length is None:
        return False
    try:
        return 0 <= int(content_length) <= FORM_BODY_MAX_BYTES
    except ValueError:
        return False


def _json_method_override(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("_method")
    return _form_method_override([value] if isinstance(value, str) else [])


def _form_method_override_from_form(form_data: object) -> str | None:
    getlist = getattr(form_data, "getlist", None)
    if not callable(getlist):  # pragma: no cover - defensive
        return None
    values = getlist("_method")
    if not all(isinstance(value, str) for value in values):
        return None
    return _form_method_override(values)


def _form_method_override(values: list[str]) -> str | None:
    if len(values) != 1:
        return None
    method = values[0].strip().upper()
    return method if method in _ALLOWED_METHODS else None


def _replay_receive(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


__all__ = [
    "MethodOverrideMiddleware",
]
