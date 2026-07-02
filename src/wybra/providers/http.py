from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from http.client import HTTPException as HTTPClientException
from http.client import HTTPSConnection
from typing import cast
from urllib.parse import SplitResult, urlsplit, urlunsplit


def https_endpoint(
    value: str,
    *,
    error_type: type[Exception],
    error_message: str,
) -> SplitResult:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise error_type(error_message)
    return parsed


def https_request(
    parsed_endpoint: SplitResult,
    *,
    method: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
    error_type: type[Exception],
    error_message: str,
) -> bytes:
    hostname = parsed_endpoint.hostname
    if hostname is None:
        raise error_type(error_message)
    connection = HTTPSConnection(
        hostname,
        parsed_endpoint.port,
        timeout=timeout,
    )
    try:
        connection.request(
            method,
            urlunsplit(
                ("", "", parsed_endpoint.path or "/", parsed_endpoint.query, "")
            ),
            body=body,
            headers=dict(headers),
        )
        response = connection.getresponse()
        response_body = response.read()
    except (HTTPClientException, OSError, TimeoutError) as exc:
        raise error_type(error_message) from exc
    finally:
        connection.close()

    if response.status < 200 or response.status >= 300:
        raise error_type(error_message)
    return response_body


def json_object_response(
    response_body: bytes,
    *,
    error_type: type[Exception],
    invalid_json_message: str,
    invalid_payload_message: str,
) -> Mapping[str, object]:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(invalid_json_message) from exc
    if not isinstance(payload, dict):
        raise error_type(invalid_payload_message)
    return cast(Mapping[str, object], payload)


def json_array_response(
    response_body: bytes,
    *,
    error_type: type[Exception],
    invalid_json_message: str,
    invalid_payload_message: str,
) -> Sequence[object]:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(invalid_json_message) from exc
    if not isinstance(payload, list):
        raise error_type(invalid_payload_message)
    return payload


def mapping_items(items: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    mappings: list[Mapping[str, object]] = []
    for item in items:
        if isinstance(item, Mapping):
            mappings.append(cast(Mapping[str, object], item))
    return tuple(mappings)


__all__ = (
    "https_endpoint",
    "https_request",
    "json_array_response",
    "json_object_response",
    "mapping_items",
)
