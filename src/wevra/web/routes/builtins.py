"""Reusable web foundation route surface."""

from fastapi import APIRouter, Depends

from wevra.web.forms.csrf import validate_csrf
from wevra.web.routes.contracts import API_PATH_PREFIX, PARTIAL_PATH_PREFIX
from wevra.web.theme import (
    THEME_API_ROUTE_NAME,
    THEME_MODE_ROUTE_NAME,
    THEME_STATUS_ROUTE_NAME,
    theme_mode_partial,
    theme_state,
    theme_status_partial,
)

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

api_router = APIRouter(prefix=f"{API_PATH_PREFIX.rstrip('/')}/web")
api_router.add_api_route(
    "/theme",
    theme_state,
    methods=["GET"],
    include_in_schema=False,
    name=THEME_API_ROUTE_NAME,
)

module_routers = {
    "partials": partial_router,
    "api": api_router,
}

__all__ = [
    "api_router",
    "module_routers",
    "partial_router",
]
