from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import uvicorn

UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


@dataclass(frozen=True, slots=True)
class ClientEndpoint:
    host: str
    port: int | None = None


def build_uvicorn_args(
    *,
    app_target: str,
    host: str,
    port: int,
    reload_enabled: bool,
    extra_args: Sequence[str],
    log_config_path: Path | None = None,
) -> list[str]:
    args = [
        app_target,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload_enabled:
        args.append("--reload")
    if log_config_path is not None:
        args.extend(("--log-config", log_config_path.as_posix()))
    args.extend(extra_args)
    return args


def reject_extra_app_target(
    extra_args: Sequence[str],
    *,
    app_target: str,
) -> None:
    for arg in extra_args:
        if not _looks_like_app_target(arg):
            continue
        if _same_app_target(arg, app_target):
            continue
        raise click.UsageError(
            "wybra-runserver owns the Uvicorn app target; pass Uvicorn options "
            "after `--`, not another app target."
        )


def uvicorn_logging_config(logging_config: Mapping[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(dict(logging_config))
    root = config.get("root")
    root_handlers = ["console"]
    if isinstance(root, Mapping):
        configured_handlers = root.get("handlers")
        if isinstance(configured_handlers, list) and configured_handlers:
            root_handlers = [
                handler for handler in configured_handlers if isinstance(handler, str)
            ] or root_handlers

    loggers = config.setdefault("loggers", {})
    if not isinstance(loggers, dict):
        loggers = {}
        config["loggers"] = loggers

    for logger_name in UVICORN_LOGGER_NAMES:
        loggers.setdefault(
            logger_name,
            {
                "handlers": root_handlers,
                "level": "INFO",
                "propagate": False,
            },
        )
    return config


def run_uvicorn_command(
    args: Sequence[str],
    *,
    logging_config: Mapping[str, Any],
) -> None:
    with uvicorn_log_config_file(logging_config) as log_config_path:
        uvicorn.main.main(
            args=[
                *build_uvicorn_args_from_existing(
                    args,
                    log_config_path=log_config_path,
                )
            ],
            prog_name="uvicorn",
        )


def build_uvicorn_args_from_existing(
    args: Sequence[str],
    *,
    log_config_path: Path,
) -> list[str]:
    configured_args = list(args)
    if "--log-config" in configured_args:
        return configured_args
    return [*configured_args, "--log-config", log_config_path.as_posix()]


@contextmanager
def uvicorn_log_config_file(logging_config: Mapping[str, Any]):
    uvicorn_config = uvicorn_logging_config(logging_config)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    ) as config_file:
        json.dump(uvicorn_config, config_file)
        config_path = Path(config_file.name)
    try:
        yield config_path
    finally:
        config_path.unlink(missing_ok=True)


def effective_client_endpoint(
    *,
    client: tuple[str, int] | None,
    headers: Sequence[tuple[bytes, bytes]],
    trusted_proxy: bool,
) -> ClientEndpoint | None:
    direct = _direct_client_endpoint(client)
    if not trusted_proxy:
        return direct

    return (
        _forwarded_client_endpoint(
            headers, fallback_port=direct.port if direct else None
        )
        or direct
    )


def _direct_client_endpoint(client: tuple[str, int] | None) -> ClientEndpoint | None:
    if client is None:
        return None
    host, port = client
    return ClientEndpoint(host=host, port=port)


def _forwarded_client_endpoint(
    headers: Sequence[tuple[bytes, bytes]],
    *,
    fallback_port: int | None,
) -> ClientEndpoint | None:
    header_map = _headers_by_name(headers)
    forwarded = header_map.get("forwarded")
    if forwarded is not None:
        endpoint = _endpoint_from_forwarded(forwarded, fallback_port=fallback_port)
        if endpoint is not None:
            return endpoint

    x_forwarded_for = header_map.get("x-forwarded-for")
    if x_forwarded_for is None:
        return None
    first_host = x_forwarded_for.split(",", 1)[0].strip()
    if not first_host:
        return None
    return _endpoint_from_host(first_host, fallback_port=fallback_port)


def _headers_by_name(headers: Sequence[tuple[bytes, bytes]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers:
        result[name.decode("latin1").lower()] = value.decode("latin1")
    return result


def _endpoint_from_forwarded(
    value: str,
    *,
    fallback_port: int | None,
) -> ClientEndpoint | None:
    first_entry = value.split(",", 1)[0]
    for part in first_entry.split(";"):
        name, separator, raw_value = part.strip().partition("=")
        if separator and name.lower() == "for":
            return _endpoint_from_host(
                raw_value.strip().strip('"'),
                fallback_port=fallback_port,
            )
    return None


def _endpoint_from_host(
    value: str, *, fallback_port: int | None
) -> ClientEndpoint | None:
    host = value.strip()
    if not host or host.lower() == "unknown":
        return None
    if host.startswith("["):
        address, separator, port_text = host.partition("]")
        if not separator:
            return None
        endpoint_host = address.removeprefix("[")
        endpoint_port = _port_from_text(port_text.removeprefix(":")) or fallback_port
        return ClientEndpoint(host=endpoint_host, port=endpoint_port)
    if ":" not in host:
        return ClientEndpoint(host=host, port=fallback_port)
    endpoint_host, port_text = host.rsplit(":", 1)
    endpoint_port = _port_from_text(port_text)
    if endpoint_port is None:
        return ClientEndpoint(host=host, port=fallback_port)
    return ClientEndpoint(host=endpoint_host, port=endpoint_port)


def _port_from_text(value: str) -> int | None:
    if not value:
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    if not 0 < port <= 65535:
        return None
    return port


def _parse_app_target(target: str) -> tuple[str, str | None]:
    module, separator, attribute = target.partition(":")
    return module, attribute if separator else None


def _same_app_target(target: str, default: str) -> bool:
    return _parse_app_target(target) == _parse_app_target(default)


def _looks_like_app_target(value: str) -> bool:
    if value.startswith("-"):
        return False

    module_name, separator, _attribute = value.partition(":")
    if separator != ":":
        return False

    module_parts = module_name.split(".")
    return bool(module_parts) and all(part.isidentifier() for part in module_parts)


__all__ = (
    "ClientEndpoint",
    "UVICORN_LOGGER_NAMES",
    "build_uvicorn_args",
    "build_uvicorn_args_from_existing",
    "effective_client_endpoint",
    "reject_extra_app_target",
    "run_uvicorn_command",
    "uvicorn_log_config_file",
    "uvicorn_logging_config",
)
