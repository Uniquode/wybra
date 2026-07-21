"""Command-line client for the Wybra diagnostics WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlsplit

import click
from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    WebSocketException,
)


class WebSocketClient(Protocol):
    """Minimal asynchronous WebSocket client contract used by the CLI."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class DebugClientError(Exception):
    """Base error for a diagnostics CLI operation."""


class DebugConnectionError(DebugClientError):
    """Raised when the diagnostics WebSocket cannot be used."""


class DebugProtocolError(DebugClientError):
    """Raised when the target does not follow the diagnostics JSON-RPC contract."""


class DebugStreamError(DebugClientError):
    """Raised when an established diagnostics stream is lost."""


class JsonRpcConnection:
    """Small JSON-RPC 2.0 client over one already-open WebSocket."""

    def __init__(self, websocket: WebSocketClient) -> None:
        self._websocket = websocket
        self._next_request_id = 1

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> str:
        request_id = self._next_request_id
        self._next_request_id += 1
        await self._websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                },
                separators=(",", ":"),
            )
        )
        raw, response = await self._receive_message()
        if "method" in response or "params" in response:
            raise DebugProtocolError("Received an invalid JSON-RPC response.")
        response_id = response.get("id")
        if type(response_id) is not int:
            raise DebugProtocolError("Received an invalid JSON-RPC response.")
        if response_id != request_id:
            raise DebugProtocolError(
                "Received a response with an unexpected request ID."
            )
        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise DebugProtocolError("Received an invalid JSON-RPC response.")
        if has_error:
            error = response["error"]
            if not isinstance(error, dict):
                raise DebugProtocolError("Received an invalid JSON-RPC response.")
            raise DebugProtocolError(_format_jsonrpc_error(error))
        return raw

    async def notifications(self) -> AsyncIterator[str]:
        while True:
            raw, message = await self._receive_message()
            if message.get("method") != "diagnostics.notification":
                raise DebugProtocolError(
                    "Received an unexpected JSON-RPC notification."
                )
            if not isinstance(message.get("params"), dict):
                raise DebugProtocolError(
                    "Received a diagnostics notification without parameters."
                )
            yield raw

    async def _receive_message(self) -> tuple[str, dict[str, object]]:
        raw = await self._websocket.recv()
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DebugProtocolError(
                    "Received a non-UTF-8 binary WebSocket message."
                ) from exc
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DebugProtocolError("Received malformed JSON-RPC text.") from exc
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise DebugProtocolError("Received an invalid JSON-RPC message.")
        return raw, message


def _format_jsonrpc_error(error: object) -> str:
    if not isinstance(error, dict):
        return "Received an invalid JSON-RPC error response."
    code = error.get("code")
    message = error.get("message")
    if isinstance(code, int) and isinstance(message, str):
        return f"JSON-RPC error {code}: {message}"
    return "Received an invalid JSON-RPC error response."


@asynccontextmanager
async def _connect(url: str) -> AsyncIterator[JsonRpcConnection]:
    try:
        websocket = await connect(url)
    except (OSError, WebSocketException) as exc:
        raise DebugConnectionError(f"Unable to connect to {url}: {exc}") from exc
    try:
        yield JsonRpcConnection(websocket)
    finally:
        await websocket.close()


def _validate_websocket_url(
    _ctx: click.Context, _param: click.Parameter, value: str
) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        valid_port = False
    else:
        valid_port = port != 0
    if not (
        parsed.scheme in {"ws", "wss"}
        and parsed.hostname
        and parsed.path
        and valid_port
    ):
        raise click.BadParameter(
            "WebSocket URL must include a supported scheme, host, port, and path."
        )
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
        raise click.BadParameter(
            "Unencrypted WebSocket URLs are permitted only for loopback endpoints; "
            "use WSS for remote diagnostics."
        )
    return value


def _is_loopback_host(hostname: str) -> bool:
    """Return whether hostname is a literal or conventional loopback host."""

    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


@click.command(
    name="wybra-debug",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
    help="Stream diagnostics from an enabled Wybra debug WebSocket.",
)
@click.argument("url", callback=_validate_websocket_url)
@click.option(
    "--list-scopes", is_flag=True, help="List available diagnostic scopes and exit."
)
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="Diagnostic scope to stream; repeat as needed.",
)
def debug_command(url: str, list_scopes: bool, scopes: tuple[str, ...]) -> None:
    """Connect to URL and list or stream selected diagnostic scopes."""

    if list_scopes and scopes:
        raise click.UsageError("--list-scopes cannot be combined with --scope.")
    if not list_scopes and not scopes:
        raise click.UsageError("Select at least one --scope or use --list-scopes.")
    try:
        asyncio.run(_run_debug(url=url, list_scopes=list_scopes, scopes=scopes))
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        return
    except DebugClientError as exc:
        raise click.ClickException(str(exc)) from exc


async def _run_debug(
    *,
    url: str,
    list_scopes: bool,
    scopes: tuple[str, ...],
) -> None:
    try:
        async with _connect(url) as connection:
            if list_scopes:
                click.echo(await connection.request("diagnostics.scopes"))
                return
            await connection.request("diagnostics.subscribe", {"scopes": list(scopes)})
            async for notification in connection.notifications():
                click.echo(notification)
    except ConnectionClosedOK:
        return
    except ConnectionClosed as exc:
        raise DebugStreamError(f"Connection to {url} was lost: {exc}") from exc


main = debug_command


__all__ = ("debug_command", "main")


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess test.
    debug_command()
