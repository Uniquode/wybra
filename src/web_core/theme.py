import json
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from web_core.csrf import request_form_data
from web_core.renderer import TemplateRenderer
from web_core.routing import HtmlView
from web_core.views import ContextBuilder, TemplateView

ThemeMode = Literal["auto", "light", "dark"]
THEME_MODES: tuple[ThemeMode, ...] = ("auto", "light", "dark")
THEME_MODE_COOKIE = "theme_mode"
THEME_STATUS_ROUTE_NAME = "web_core:partial:theme-selector"
THEME_MODE_ROUTE_NAME = "web_core:partial:theme-mode"
THEME_API_ROUTE_NAME = "web_core:api:theme"
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

    return {"theme_return_path": return_path}


@dataclass(frozen=True, slots=True)
class ThemeStatusPartialView(TemplateView):
    template_name: str = THEME_STATUS_TEMPLATE
    context_builder: ContextBuilder | None = theme_return_context


@dataclass(frozen=True, slots=True)
class ThemeModePartialView(HtmlView):
    async def render(self, request: Request, renderer: TemplateRenderer) -> Response:
        try:
            form_data = await request_form_data(request)
            submitted_mode = form_data.get("theme_mode", "auto")
            return_path = form_data.get("return_to", "/")
        except AssertionError:
            body = (await request.body()).decode("utf-8")
            parsed_form_data = parse_qs(body, keep_blank_values=True)
            submitted_mode = next(
                iter(parsed_form_data.get("theme_mode", ["auto"])), "auto"
            )
            return_path = next(iter(parsed_form_data.get("return_to", ["/"])), "/")

        theme_mode = normalise_theme_mode(
            submitted_mode if isinstance(submitted_mode, str) else "auto"
        )
        redirect_path = (
            return_path if isinstance(return_path, str) and return_path else "/"
        )

        if request.headers.get("HX-Request") != "true":
            response = RedirectResponse(url=redirect_path, status_code=303)
            set_theme_mode_cookie(response, theme_mode)
            return response

        context = theme_template_context(
            request, theme_mode=theme_mode
        ) | theme_return_context(request, return_path=redirect_path)
        response = renderer.render_partial(THEME_STATUS_TEMPLATE, request, context)
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
    "ThemeModePartialView",
    "ThemeStatusPartialView",
    "next_theme_mode",
    "normalise_theme_mode",
    "resolve_theme_mode",
    "set_theme_mode_cookie",
    "set_theme_mode_trigger",
    "theme_return_context",
    "theme_state",
    "theme_template_context",
]
