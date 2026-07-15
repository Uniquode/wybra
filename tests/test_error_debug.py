from __future__ import annotations

from fastapi import FastAPI, Request

from wybra.errors.debug import _chained_errors, development_error_context
from wybra.testing import WybraTestClient


def test_development_error_context_includes_request_route_and_traceback() -> None:
    app = FastAPI()
    captured = {}

    @app.get("/debug/{item_id}", name="debug_item")
    async def debug_item(request: Request, item_id: str) -> dict[str, object]:
        try:
            raise RuntimeError(f"missing {item_id}")
        except RuntimeError as exc:
            captured["context"] = development_error_context(request, exc)
        return {"ok": True}

    with WybraTestClient(app) as client:
        response = client.get("/debug/abc?mode=detail")

    assert response.status_code == 200
    context = captured["context"]
    assert context.method == "GET"
    assert context.path == "/debug/abc"
    assert context.query == "mode=detail"
    assert context.route_name == "debug_item"
    assert context.endpoint == "debug_item"
    assert context.exception_type == "RuntimeError"
    assert context.exception_message == "missing abc"
    assert any(frame.function == "debug_item" for frame in context.traceback)


def test_development_error_context_includes_chained_causes() -> None:
    app = FastAPI()

    @app.get("/debug")
    async def debug(request: Request) -> dict[str, object]:
        try:
            try:
                raise LookupError("original")
            except LookupError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as exc:
            context = development_error_context(request, exc)
        return {
            "causes": [
                {"type": cause.exception_type, "message": cause.exception_message}
                for cause in context.causes
            ]
        }

    with WybraTestClient(app) as client:
        response = client.get("/debug")

    assert response.json() == {
        "causes": [{"type": "LookupError", "message": "original"}]
    }


def test_chained_error_collection_stops_at_cycles() -> None:
    first = RuntimeError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    causes = _chained_errors(first)

    assert [(cause.exception_type, cause.exception_message) for cause in causes] == [
        ("ValueError", "second")
    ]
