from __future__ import annotations

from dataclasses import dataclass
from traceback import walk_tb

from fastapi import Request


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    module: str
    function: str
    filename: str
    line_number: int


@dataclass(frozen=True, slots=True)
class ChainedError:
    exception_type: str
    exception_message: str


@dataclass(frozen=True, slots=True)
class DevelopmentErrorContext:
    method: str
    path: str
    query: str
    route_name: str | None
    endpoint: str | None
    exception_type: str
    exception_message: str
    traceback: tuple[ErrorFrame, ...]
    causes: tuple[ChainedError, ...]


def development_error_context(
    request: Request,
    exc: BaseException,
) -> DevelopmentErrorContext:
    return DevelopmentErrorContext(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        route_name=_route_name(request),
        endpoint=_endpoint_name(request),
        exception_type=type(exc).__name__,
        exception_message=str(exc),
        traceback=_traceback_frames(exc),
        causes=_chained_errors(exc),
    )


def _route_name(request: Request) -> str | None:
    route = request.scope.get("route")
    name = getattr(route, "name", None)
    return name if isinstance(name, str) and name else None


def _endpoint_name(request: Request) -> str | None:
    endpoint = request.scope.get("endpoint")
    name = getattr(endpoint, "__name__", None)
    return name if isinstance(name, str) and name else None


def _traceback_frames(exc: BaseException) -> tuple[ErrorFrame, ...]:
    if exc.__traceback__ is None:
        return ()
    return tuple(
        ErrorFrame(
            module=str(frame.f_globals.get("__name__", "")),
            function=frame.f_code.co_name,
            filename=frame.f_code.co_filename,
            line_number=line_number,
        )
        for frame, line_number in walk_tb(exc.__traceback__)
    )


def _chained_errors(exc: BaseException) -> tuple[ChainedError, ...]:
    causes: list[ChainedError] = []
    current = exc.__cause__ or exc.__context__
    while current is not None:
        causes.append(
            ChainedError(
                exception_type=type(current).__name__,
                exception_message=str(current),
            )
        )
        current = current.__cause__ or current.__context__
    return tuple(causes)
