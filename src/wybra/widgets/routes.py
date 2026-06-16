from __future__ import annotations

from fastapi import APIRouter, Depends

from wybra.web.forms.csrf import validate_csrf
from wybra.web.routes.contracts import API_PATH_PREFIX, PARTIAL_PATH_PREFIX
from wybra.widgets.config import THEME_FEATURE
from wybra.widgets.theme import (
    THEME_API_ROUTE_NAME,
    THEME_MODE_ROUTE_NAME,
    THEME_STATUS_ROUTE_NAME,
    theme_mode_partial,
    theme_state,
    theme_status_partial,
)

module_routers: dict[str, APIRouter] = {}


def configure_routes(features: tuple[str, ...]) -> None:
    global module_routers
    module_routers = _module_routers(features)


def _module_routers(features: tuple[str, ...]) -> dict[str, APIRouter]:
    if THEME_FEATURE not in features:
        return {}

    partial_router = APIRouter(dependencies=[Depends(validate_csrf)])
    partial_router.add_api_route(
        f"{PARTIAL_PATH_PREFIX}/theme-selector",
        theme_status_partial,
        methods=["GET"],
        include_in_schema=False,
        name=THEME_STATUS_ROUTE_NAME,
    )
    partial_router.add_api_route(
        f"{PARTIAL_PATH_PREFIX}/theme-mode",
        theme_mode_partial,
        methods=["POST"],
        include_in_schema=False,
        name=THEME_MODE_ROUTE_NAME,
    )

    api_router = APIRouter(prefix=f"{API_PATH_PREFIX.rstrip('/')}/widgets")
    api_router.add_api_route(
        "/theme",
        theme_state,
        methods=["GET"],
        include_in_schema=False,
        name=THEME_API_ROUTE_NAME,
    )
    return {
        "partials": partial_router,
        "api": api_router,
    }


configure_routes((THEME_FEATURE,))

__all__ = (
    "configure_routes",
    "module_routers",
)
