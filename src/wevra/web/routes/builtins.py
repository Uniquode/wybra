"""Reusable web foundation route surface."""

from fastapi import APIRouter

from wevra.web.routes.contracts import API_PATH_PREFIX, PARTIAL_PATH_PREFIX
from wevra.web.routes.registration import HtmlRouteDefinition, ModuleRoutes
from wevra.web.theme import (
    THEME_API_ROUTE_NAME,
    THEME_MODE_ROUTE_NAME,
    THEME_STATUS_ROUTE_NAME,
    ThemeModePartialView,
    ThemeStatusPartialView,
    theme_state,
)


def build_wevra_web_module_routes() -> ModuleRoutes:
    normalised_api_prefix = API_PATH_PREFIX.rstrip("/")
    api_router = APIRouter(prefix=f"{normalised_api_prefix}/web")
    api_router.add_api_route(
        "/theme",
        theme_state,
        methods=["GET"],
        include_in_schema=False,
        name=THEME_API_ROUTE_NAME,
    )

    return ModuleRoutes(
        partial_routes=(
            HtmlRouteDefinition(
                path=f"{PARTIAL_PATH_PREFIX}/theme-selector",
                name=THEME_STATUS_ROUTE_NAME,
                methods=("GET",),
                surface="partial",
                view=ThemeStatusPartialView(),
            ),
            HtmlRouteDefinition(
                path=f"{PARTIAL_PATH_PREFIX}/theme-mode",
                name=THEME_MODE_ROUTE_NAME,
                methods=("POST",),
                surface="partial",
                view=ThemeModePartialView(),
            ),
        ),
        api_routers=(api_router,),
    )


module_routes = build_wevra_web_module_routes()

__all__ = [
    "build_wevra_web_module_routes",
    "module_routes",
]
