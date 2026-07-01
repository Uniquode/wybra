from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import Request
from starlette.routing import NoMatchFound


def normalise_return_to(value: str | None, default: str = "/account") -> str:
    candidate = (value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        return default

    decoded_candidate = unquote(candidate)
    if (
        decoded_candidate.startswith("//")
        or "\\" in decoded_candidate
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in decoded_candidate
        )
    ):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default

    return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def optional_route_path(request: Request, route_name: str) -> str | None:
    try:
        return urlsplit(str(request.url_for(route_name))).path
    except NoMatchFound:
        return None


def route_path(request: Request, route_name: str) -> str:
    path = optional_route_path(request, route_name)
    if path is None:
        raise NoMatchFound(route_name, {})
    return path
