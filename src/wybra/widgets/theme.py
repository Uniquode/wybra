import json
from typing import Literal, cast
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from wybra.template import render_partial
from wybra.web.forms.csrf import request_form_data

ThemeMode = Literal["auto", "light", "dark"]
THEME_MODES: tuple[ThemeMode, ...] = ("auto", "light", "dark")
THEME_MODE_COOKIE = "theme_mode"
THEME_STATUS_ROUTE_NAME = "wybra.widgets:partial:theme-selector"
THEME_MODE_ROUTE_NAME = "wybra.widgets:partial:theme-mode"
THEME_API_ROUTE_NAME = "wybra.widgets:api:theme"
THEME_STATUS_TEMPLATE = "components/theme_selector.html"
THEME_MODE_ICONS: dict[ThemeMode, str] = {
    "auto": "computer",
    "light": "light_mode",
    "dark": "dark_mode",
}


def normalise_theme_mode(value: str | None) -> ThemeMode:
    if value in THEME_MODES:
        return cast(ThemeMode, value)

    return "auto"


def normalise_theme_return_path(value: str | None, default: str = "/") -> str:
    candidate = (value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default

    return candidate


def resolve_theme_mode(request: Request) -> ThemeMode:
    return normalise_theme_mode(request.cookies.get(THEME_MODE_COOKIE))


def next_theme_mode(theme_mode: ThemeMode) -> ThemeMode:
    current_index = THEME_MODES.index(theme_mode)
    return THEME_MODES[(current_index + 1) % len(THEME_MODES)]


def set_theme_mode_cookie(response: Response, theme_mode: ThemeMode) -> None:
    response.set_cookie(
        THEME_MODE_COOKIE,
        theme_mode,
        httponly=True,
        max_age=31_536_000,
        path="/",
        samesite="lax",
    )


def set_theme_mode_trigger(response: Response, theme_mode: ThemeMode) -> None:
    response.headers["HX-Trigger"] = json.dumps(
        {"theme-mode-changed": {"theme_mode": theme_mode}}
    )


def theme_template_context(
    request: Request, *, theme_mode: ThemeMode | None = None
) -> dict[str, str]:
    current_theme = theme_mode or resolve_theme_mode(request)
    return {
        "theme_mode": current_theme,
        "theme_label": current_theme.title(),
        "theme_attribute": "" if current_theme == "auto" else current_theme,
        "theme_next_mode": next_theme_mode(current_theme),
        "theme_icon_name": THEME_MODE_ICONS[current_theme],
    }


async def theme_state(request: Request) -> dict[str, str]:
    return {"theme_mode": resolve_theme_mode(request)}


def theme_return_context(
    request: Request, *, return_path: str | None = None
) -> dict[str, str]:
    del request
    if return_path is None:
        return {}

    return {"theme_return_path": normalise_theme_return_path(return_path)}


def _form_value(form_data: FormData, name: str, default: str) -> str:
    value = form_data.get(name, default)
    return value if isinstance(value, str) else default


async def _theme_form_values(request: Request) -> tuple[str, str]:
    try:
        form_data = await request_form_data(request)
    except Exception:
        parsed_form_data = await _parse_urlencoded_body(request)
        return (
            _first_parsed_form_value(parsed_form_data, "theme_mode", "auto"),
            _first_parsed_form_value(parsed_form_data, "return_to", "/"),
        )

    return (
        _form_value(form_data, "theme_mode", "auto"),
        _form_value(form_data, "return_to", "/"),
    )


async def _parse_urlencoded_body(request: Request) -> dict[str, list[str]]:
    try:
        body = await request.body()
    except Exception:
        return {}

    return parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)


def _first_parsed_form_value(
    parsed_form_data: dict[str, list[str]],
    name: str,
    default: str,
) -> str:
    values = parsed_form_data.get(name)
    if not values:
        return default

    return values[0]


async def theme_status_partial(request: Request) -> Response:
    return render_partial(request, THEME_STATUS_TEMPLATE, theme_return_context(request))


async def theme_mode_partial(request: Request) -> Response:
    submitted_mode, return_path = await _theme_form_values(request)
    theme_mode = normalise_theme_mode(submitted_mode)
    redirect_path = normalise_theme_return_path(return_path)

    if request.headers.get("HX-Request") != "true":
        response = RedirectResponse(url=redirect_path, status_code=303)
        set_theme_mode_cookie(response, theme_mode)
        return response

    context = theme_template_context(
        request, theme_mode=theme_mode
    ) | theme_return_context(request, return_path=redirect_path)
    response = render_partial(request, THEME_STATUS_TEMPLATE, context)
    set_theme_mode_cookie(response, theme_mode)
    set_theme_mode_trigger(response, theme_mode)
    return response


__all__ = [
    "THEME_API_ROUTE_NAME",
    "THEME_MODE_COOKIE",
    "THEME_MODE_ICONS",
    "THEME_MODE_ROUTE_NAME",
    "THEME_MODES",
    "THEME_STATUS_ROUTE_NAME",
    "THEME_STATUS_TEMPLATE",
    "ThemeMode",
    "next_theme_mode",
    "normalise_theme_mode",
    "normalise_theme_return_path",
    "resolve_theme_mode",
    "set_theme_mode_cookie",
    "set_theme_mode_trigger",
    "theme_return_context",
    "theme_mode_partial",
    "theme_state",
    "theme_status_partial",
    "theme_template_context",
]
