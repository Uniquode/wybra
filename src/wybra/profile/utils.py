"""Profile-level helpers shared by forms, routes, and widgets."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


def normalise_form_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


def normalise_return_to(value: str | None, *, default: str) -> str:
    candidate = (value or "").strip()
    if not candidate.startswith("/"):
        return default

    decoded = unquote(candidate)
    if decoded.startswith("//") or decoded.startswith("/\\"):
        return default
    if any(character in decoded for character in ("\r", "\n", "\x00")):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def extract_return_to_query(query: str | None) -> str | None:
    if not isinstance(query, str) or not query:
        return None
    values = parse_qs(query, keep_blank_values=False).get("return_to", ())
    value = values[0] if values else None
    return value if isinstance(value, str) and value.startswith("/") else None


def validate_safe_url(url: str) -> None:
    from wybra.profile.exceptions import ProfileInputError

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileInputError("Profile link URL scheme must be http or https.")
    if not parsed.netloc:
        raise ProfileInputError("Profile link URL must include a host.")
    if contains_control_character(url):
        raise ProfileInputError("Profile link URL must not contain control characters.")


def contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
