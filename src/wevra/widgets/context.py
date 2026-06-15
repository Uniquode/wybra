from __future__ import annotations

from typing import Any

from starlette.routing import NoMatchFound

from wevra.web.context import TemplateContext, add_to_context, clear_context_providers
from wevra.widgets.config import LOGIN_FEATURE, THEME_FEATURE
from wevra.widgets.login import login_widget_state
from wevra.widgets.theme import THEME_MODE_ROUTE_NAME, theme_template_context


def widgets_theme_context(
    request: Any,
    context: TemplateContext,
) -> TemplateContext:
    values = theme_template_context(request)
    try:
        theme_update_path = str(request.url_for(THEME_MODE_ROUTE_NAME))
    except NoMatchFound:
        return context.merge(values)

    return context.merge(
        values
        | {
            "theme_update_path": theme_update_path,
            "theme_return_path": request.url.path,
        }
    )


async def widgets_login_context(
    request: Any,
    context: TemplateContext,
) -> TemplateContext:
    state = await login_widget_state(request)
    if state is None:
        return context
    return context.with_values(login_widget=state)


def configure_context(features: tuple[str, ...]) -> None:
    clear_context_providers(__name__)
    if LOGIN_FEATURE in features:
        add_to_context(widgets_login_context)
    if THEME_FEATURE in features:
        add_to_context(widgets_theme_context)


configure_context((THEME_FEATURE,))

__all__ = (
    "configure_context",
    "widgets_login_context",
    "widgets_theme_context",
)
