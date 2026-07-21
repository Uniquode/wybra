"""Guarded JSON-RPC WebSocket adapter for runtime diagnostics."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import suppress
from typing import cast
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from wybra.content_types import ContentTypesCapability
from wybra.diagnostics.capabilities import (
    DiagnosticsCapability,
    DiagnosticsSubscription,
)
from wybra.diagnostics.settings import DiagnosticsSettings
from wybra.events._core import (
    EVT_CONTENT_TYPES,
    EventScope,
    EventScopeError,
    available_event_scopes,
    parse_event_scopes,
)
from wybra.site import Site

DEBUG_WEBSOCKET_PATH = "/__debug/ws"
_PARSE_ERROR = object()
_RESOLVED_HOSTS: dict[str, frozenset[str]] = {}


def register_debug_websocket(
    site: Site,
    settings: DiagnosticsSettings,
    capability: DiagnosticsCapability,
) -> None:
    """Register the local diagnostics endpoint after explicit activation."""

    if not settings.events_enabled or not settings.debug_enabled:
        return

    @site.app.websocket(DEBUG_WEBSOCKET_PATH)
    async def diagnostics_websocket(websocket: WebSocket) -> None:
        if not await _peer_is_allowed(websocket, settings.debug_allowed_hosts):
            await websocket.close(code=1008)
            return
        if not _origin_is_allowed(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await _serve_connection(site, websocket, capability)


async def _serve_connection(
    site: Site,
    websocket: WebSocket,
    capability: DiagnosticsCapability,
) -> None:
    subscription: DiagnosticsSubscription | None = None
    request_task = asyncio.create_task(_receive_request(websocket))
    try:
        while True:
            notification_task = (
                asyncio.create_task(subscription.receive())
                if subscription is not None
                else None
            )
            tasks = {request_task}
            if notification_task is not None:
                tasks.add(notification_task)
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if notification_task is not None and notification_task in done:
                notification = notification_task.result().as_dict()
                if subscription is not None and subscription.take_dropped():
                    notification["dropped"] = True
                await websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "diagnostics.notification",
                        "params": notification,
                    }
                )
            if request_task in done:
                request = request_task.result()
                if request is _PARSE_ERROR:
                    await _send_error(websocket, None, -32700, "Parse error.")
                else:
                    subscription = await _handle_request(
                        site,
                        websocket,
                        capability,
                        request,
                        subscription,
                    )
                request_task = asyncio.create_task(_receive_request(websocket))
            if notification_task is not None and notification_task in pending:
                notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await notification_task
    except WebSocketDisconnect:
        pass
    finally:
        if subscription is not None:
            capability.unsubscribe(subscription)


async def _handle_request(
    site: Site,
    websocket: WebSocket,
    capability: DiagnosticsCapability,
    request: object,
    subscription: DiagnosticsSubscription | None,
) -> DiagnosticsSubscription | None:
    if not isinstance(request, dict):
        await _send_error(websocket, None, -32600, "Invalid JSON-RPC request.")
        return subscription
    request = cast(dict[str, object], request)
    request_id = request.get("id")
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        await _send_error(websocket, request_id, -32600, "Invalid JSON-RPC request.")
        return subscription
    params = request.get("params", {})
    if not isinstance(params, dict):
        await _send_error(
            websocket,
            request_id,
            -32602,
            "Parameters must be an object.",
        )
        return subscription
    params = cast(dict[str, object], params)
    try:
        if method == "diagnostics.scopes":
            result = [
                {"name": str(scope), "description": description}
                for scope, description in available_event_scopes()
            ]
        elif method == "rpc.discover":
            result = _openrpc_document()
        elif method == "diagnostics.snapshot":
            scope = _one_scope(params)
            result = _snapshot_result(site, capability, scope)
        elif method == "diagnostics.subscribe":
            scopes = _scopes(params)
            if EVT_CONTENT_TYPES in scopes:
                raise ValueError(
                    "content_types is a snapshot-only diagnostic resource."
                )
            replacement = await capability.subscribe(scopes)
            if subscription is not None:
                capability.unsubscribe(subscription)
            subscription = replacement
            result = {"subscribed": [str(scope) for scope in subscription.scopes]}
        elif method == "diagnostics.unsubscribe":
            if subscription is not None:
                capability.unsubscribe(subscription)
                subscription = None
            result = {"subscribed": False}
        else:
            await _send_error(websocket, request_id, -32601, "Method not found.")
            return subscription
    except (EventScopeError, ValueError) as exc:
        await _send_error(websocket, request_id, -32602, str(exc))
        return subscription
    await websocket.send_json({"jsonrpc": "2.0", "id": request_id, "result": result})
    return subscription


async def _receive_request(websocket: WebSocket) -> object:
    try:
        return await websocket.receive_json()
    except json.JSONDecodeError:
        return _PARSE_ERROR


def _one_scope(params: dict[str, object]) -> EventScope:
    value = params.get("scope")
    if not isinstance(value, str):
        raise EventScopeError("Snapshot requests require one string scope.")
    scopes = parse_event_scopes(value)
    if len(scopes) != 1:
        raise EventScopeError("Snapshot requests require exactly one scope.")
    return scopes[0]


def _scopes(params: dict[str, object]) -> tuple[EventScope, ...]:
    value = params.get("scopes")
    if isinstance(value, str):
        return parse_event_scopes(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return parse_event_scopes(tuple(cast(str, item) for item in value))
    raise EventScopeError("Subscriptions require a string or list of string scopes.")


def _snapshot_result(
    site: Site,
    capability: DiagnosticsCapability,
    scope: EventScope,
) -> list[dict[str, object]]:
    if str(scope) == "content_types":
        content_types = site.optional_capability(ContentTypesCapability)
        if content_types is None:
            return []
        return [
            {
                "identifier": content_type.identifier,
                "model": (
                    f"{content_type.model.__module__}.{content_type.model.__name__}"
                ),
                "verbose_name": content_type.verbose_name,
                "verbose_name_plural": content_type.verbose_name_plural,
                "actions": sorted(content_type.actions),
            }
            for content_type in content_types.all()
        ]
    return [
        snapshot.as_dict()
        for snapshot in capability.snapshots(scope, include_empty=True)
    ]


def _openrpc_document() -> dict[str, object]:
    return {
        "openrpc": "1.2.6",
        "info": {
            "title": "Wybra debug API",
            "version": "0.1.0",
            "description": "Process-local observational diagnostics control plane.",
        },
        "methods": [
            {
                "name": "rpc.discover",
                "summary": "Return this OpenRPC service description.",
                "params": [],
                "result": {"name": "document", "schema": {"type": "object"}},
            },
            {
                "name": "diagnostics.scopes",
                "summary": "List available diagnostic scopes.",
                "params": [],
                "result": {"name": "scopes", "schema": {"type": "array"}},
            },
            {
                "name": "diagnostics.snapshot",
                "summary": "Retrieve retained snapshots for one scope.",
                "params": [_scope_parameter("scope")],
                "result": {"name": "snapshots", "schema": {"type": "array"}},
            },
            {
                "name": "diagnostics.subscribe",
                "summary": "Set this connection's diagnostic subscriptions.",
                "params": [
                    {
                        "name": "scopes",
                        "required": True,
                        "schema": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                    }
                ],
                "result": {
                    "name": "subscription",
                    "schema": {"type": "object"},
                },
            },
            {
                "name": "diagnostics.unsubscribe",
                "summary": "Clear this connection's diagnostic subscriptions.",
                "params": [],
                "result": {
                    "name": "subscription",
                    "schema": {"type": "object"},
                },
            },
        ],
        "x-wybra-notifications": ["diagnostics.notification"],
    }


def _scope_parameter(name: str) -> dict[str, object]:
    return {
        "name": name,
        "required": True,
        "schema": {"type": "string"},
    }


async def _send_error(
    websocket: WebSocket,
    request_id: object,
    code: int,
    message: str,
) -> None:
    await websocket.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


async def _peer_is_allowed(
    websocket: WebSocket,
    allowed_hosts: tuple[str, ...],
) -> bool:
    """Authorise the effective ASGI peer, never caller-supplied headers.

    Uvicorn applies trusted ``Forwarded``/``X-Forwarded-For`` metadata before
    this point when it is configured with ``--forwarded-allow-ips``. Therefore
    a reverse proxy may supply the real developer peer, but an untrusted direct
    client cannot spoof that peer with a request header.
    """

    client = websocket.client
    if client is None:
        return False
    peer = client.host.lower()
    for host in allowed_hosts:
        if peer == host or peer in await _resolved_hosts(host):
            return True
    return False


def _origin_is_allowed(websocket: WebSocket) -> bool:
    """Reject browser cross-origin WebSocket handshakes.

    Non-browser tools such as the future Node CLI do not normally send Origin
    and remain governed by peer authorisation. Origin is defence in depth, not
    the endpoint's network authorisation mechanism.
    """

    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    origin_authority = _origin_authority(origin)
    host_authority = _host_authority(websocket.headers.get("host", ""))
    return origin_authority is not None and origin_authority == host_authority


def _origin_authority(value: str) -> tuple[str, int | None] | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname.lower(), parsed.port


def _host_authority(value: str) -> tuple[str, int | None] | None:
    parsed = urlsplit(f"//{value}")
    if not parsed.hostname:
        return None
    return parsed.hostname.lower(), parsed.port


async def _resolved_hosts(host: str) -> frozenset[str]:
    """Resolve an allowed hostname without blocking the event loop."""

    cached = _RESOLVED_HOSTS.get(host)
    if cached is not None:
        return cached
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
        )
        resolved = frozenset(str(address[4][0]).lower() for address in addresses)
    except OSError:
        resolved = frozenset()
    _RESOLVED_HOSTS[host] = resolved
    return resolved


__all__ = ("DEBUG_WEBSOCKET_PATH", "register_debug_websocket")
