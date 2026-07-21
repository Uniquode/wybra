import asyncio
import json
import signal
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from uvicorn import Config, Server
from websockets.asyncio.server import ServerConnection, serve

from wybra import start_site
from wybra.config import MappingConfigSource
from wybra.tools import debug as debug_module
from wybra.tools.debug import DebugProtocolError, JsonRpcConnection, debug_command


class FakeWebSocket:
    def __init__(self, responses: list[str | bytes]) -> None:
        self.responses = deque(responses)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return self.responses.popleft()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000/__debug/ws",
        "ws://:8000/__debug/ws",
        "ws://127.0.0.1:not-a-port/__debug/ws",
    ],
)
def test_debug_command_rejects_invalid_websocket_target(url: str) -> None:
    result = CliRunner().invoke(
        debug_command,
        [url, "--list-scopes"],
    )

    assert result.exit_code == 2
    assert "WebSocket URL" in result.output


def test_debug_command_rejects_unencrypted_remote_websocket_target() -> None:
    result = CliRunner().invoke(
        debug_command,
        ["ws://192.0.2.1:8000/__debug/ws", "--list-scopes"],
    )

    assert result.exit_code == 2
    assert "use WSS for remote diagnostics" in result.output


@pytest.mark.anyio
async def test_jsonrpc_connection_matches_the_response_to_its_request() -> None:
    websocket = FakeWebSocket(['{"jsonrpc":"2.0","id":1,"result":[]}'])
    connection = JsonRpcConnection(websocket)

    response = await connection.request("diagnostics.scopes")

    assert response == '{"jsonrpc":"2.0","id":1,"result":[]}'
    assert websocket.sent == [
        '{"jsonrpc":"2.0","id":1,"method":"diagnostics.scopes","params":{}}'
    ]


@pytest.mark.anyio
async def test_jsonrpc_connection_rejects_an_error_response() -> None:
    websocket = FakeWebSocket(
        [
            '{"jsonrpc":"2.0","id":1,"error":'
            '{"code":-32601,"message":"Method not found."}}'
        ]
    )

    with pytest.raises(DebugProtocolError, match="-32601: Method not found."):
        await JsonRpcConnection(websocket).request("diagnostics.scopes")


@pytest.mark.anyio
async def test_jsonrpc_connection_rejects_a_response_with_an_unexpected_id() -> None:
    websocket = FakeWebSocket(['{"jsonrpc":"2.0","id":2,"result":[]}'])

    with pytest.raises(DebugProtocolError, match="unexpected request ID"):
        await JsonRpcConnection(websocket).request("diagnostics.scopes")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        '{"jsonrpc":"2.0","id":true,"result":[]}',
        '{"jsonrpc":"2.0","id":1,"error":null,"result":[]}',
        '{"jsonrpc":"2.0","id":1}',
        '{"jsonrpc":"2.0","id":1,"method":"diagnostics.notification","result":[]}',
    ],
)
async def test_jsonrpc_connection_rejects_malformed_response(
    response: str,
) -> None:
    connection = JsonRpcConnection(FakeWebSocket([response]))

    with pytest.raises(DebugProtocolError, match="invalid JSON-RPC response"):
        await connection.request("diagnostics.scopes")


@pytest.mark.anyio
async def test_jsonrpc_connection_yields_only_diagnostics_notification_frames() -> None:
    frame = (
        '{"jsonrpc":"2.0","method":"diagnostics.notification","params":{"scope":"sql"}}'
    )
    connection = JsonRpcConnection(FakeWebSocket([frame]))

    assert await anext(connection.notifications()) == frame


@pytest.mark.anyio
async def test_jsonrpc_connection_decodes_utf8_binary_notification_frames() -> None:
    frame = b'{"jsonrpc":"2.0","method":"diagnostics.notification","params":{}}'
    connection = JsonRpcConnection(FakeWebSocket([frame]))

    assert await anext(connection.notifications()) == frame.decode()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        b"\xff",
        '{"jsonrpc":"1.0","method":"diagnostics.notification","params":{}}',
        '{"jsonrpc":"2.0","method":"unexpected","params":{}}',
        '{"jsonrpc":"2.0","method":"diagnostics.notification","params":null}',
    ],
)
async def test_jsonrpc_connection_rejects_malformed_notification(
    frame: str | bytes,
) -> None:
    connection = JsonRpcConnection(FakeWebSocket([frame]))

    with pytest.raises(DebugProtocolError):
        await anext(connection.notifications())


@pytest.mark.anyio
async def test_debug_command_lists_scopes_from_a_local_websocket() -> None:
    response = '{"jsonrpc":"2.0","id":1,"result":[{"name":"sql"}]}'

    async def diagnostics_server(connection: ServerConnection) -> None:
        request = await connection.recv()
        assert isinstance(request, str)
        assert json.loads(request) == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "diagnostics.scopes",
            "params": {},
        }
        await connection.send(response)

    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--list-scopes"],
        )

    assert result.exit_code == 0
    assert result.output == f"{response}\n"


@pytest.mark.anyio
async def test_debug_command_reports_jsonrpc_errors_from_a_local_websocket() -> None:
    response = (
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found."}}'
    )

    async def diagnostics_server(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(response)

    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--list-scopes"],
        )

    assert result.exit_code == 1
    assert "JSON-RPC error -32601: Method not found." in result.output


@pytest.mark.anyio
async def test_debug_command_reports_an_unavailable_local_target() -> None:
    async def reject_connection(
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reject_connection, "127.0.0.1", 0)
    socket = server.sockets[0]
    host, port = socket.getsockname()[:2]
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--list-scopes"],
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.exit_code == 1
    assert f"Unable to connect to ws://{host}:{port}/__debug/ws" in result.output


def test_debug_command_requires_a_scope_when_not_listing_scopes() -> None:
    result = CliRunner().invoke(debug_command, ["ws://127.0.0.1:8000/__debug/ws"])

    assert result.exit_code == 2
    assert "Select at least one --scope" in result.output


def test_debug_command_rejects_scope_selection_when_listing_scopes() -> None:
    result = CliRunner().invoke(
        debug_command,
        ["ws://127.0.0.1:8000/__debug/ws", "--list-scopes", "--scope", "sql"],
    )

    assert result.exit_code == 2
    assert "--list-scopes cannot be combined with --scope" in result.output


@pytest.mark.anyio
async def test_debug_command_treats_a_graceful_stream_close_as_normal_completion() -> (
    None
):
    async def diagnostics_server(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(
            '{"jsonrpc":"2.0","id":1,"result":{"subscribed":["sql"]}}'
        )

    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--scope", "sql"],
        )

    assert result.exit_code == 0
    assert result.output == ""


@pytest.mark.anyio
async def test_debug_command_reports_a_lost_stream_as_a_stream_error() -> None:
    async def diagnostics_server(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(
            '{"jsonrpc":"2.0","id":1,"result":{"subscribed":["sql"]}}'
        )
        await connection.close(code=1011, reason="server stopped")

    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--scope", "sql"],
        )

    assert result.exit_code == 1
    assert f"Connection to ws://{host}:{port}/__debug/ws was lost" in result.output


@pytest.mark.anyio
async def test_debug_command_ends_quietly_when_stdout_pipe_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def diagnostics_server(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send('{"jsonrpc":"2.0","id":1,"result":[]}')

    def broken_pipe(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(debug_module.click, "echo", broken_pipe)
    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [f"ws://{host}:{port}/__debug/ws", "--list-scopes"],
        )

    assert result.exit_code == 0
    assert result.output == ""


def _debug_config() -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {"modules": (), "deployment_environment": "local"},
            "wybra.diagnostics": {
                "events_enabled": True,
                "debug_enabled": True,
                "debug_allowed_hosts": ("127.0.0.1",),
                "level": "trace",
            },
        }
    )


@asynccontextmanager
async def _running_debug_application() -> AsyncIterator[str]:
    app = FastAPI(lifespan=start_site(config_source=_debug_config()))
    server = Server(
        Config(app, host="127.0.0.1", port=0, access_log=False, log_level="critical")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0)
    socket = server.servers[0].sockets[0]
    host, port = socket.getsockname()[:2]
    try:
        yield f"ws://{host}:{port}/__debug/ws"
    finally:
        server.should_exit = True
        await task


@pytest.mark.anyio
async def test_debug_command_lists_scopes_from_the_wybra_debug_endpoint() -> None:
    async with _running_debug_application() as url:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            debug_command,
            [url, "--list-scopes"],
        )

    assert result.exit_code == 0
    response = json.loads(result.output)
    assert response["id"] == 1
    assert {scope["name"] for scope in response["result"]} >= {"sql", "request"}


@pytest.mark.anyio
async def test_debug_command_closes_the_stream_on_sigint() -> None:
    subscribed = asyncio.Event()
    connection_closed = asyncio.Event()

    async def diagnostics_server(connection: ServerConnection) -> None:
        try:
            request = await connection.recv()
            assert isinstance(request, str)
            assert json.loads(request) == {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "diagnostics.subscribe",
                "params": {"scopes": ["sql"]},
            }
            await connection.send(
                '{"jsonrpc":"2.0","id":1,"result":{"subscribed":["sql"]}}'
            )
            subscribed.set()
            await connection.wait_closed()
        finally:
            connection_closed.set()

    async with serve(diagnostics_server, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        interrupt_signal = (
            signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "wybra.tools.debug",
            f"ws://{host}:{port}/__debug/ws",
            "--scope",
            "sql",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        await subscribed.wait()
        process.send_signal(interrupt_signal)
        stdout, stderr = await process.communicate()
        await connection_closed.wait()

    assert process.returncode == 0
    assert stdout == b""
    assert stderr == b""
