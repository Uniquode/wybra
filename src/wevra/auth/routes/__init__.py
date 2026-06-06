"""Authentication route pages, context providers, and router wiring.

Hosts normally import ``module_routes`` or the route builders when composing
auth pages into an application-selected route prefix.
"""

from wevra.auth.routes.pages import (
    IdentityRouteSet,
    build_identity_module_routes,
    build_identity_route_set,
    current_user_state,
    module_routes,
    normalise_return_to,
)
from wevra.auth.routes.wiring import RouteReplacement, RouterExtensionPlan

__all__ = [
    "IdentityRouteSet",
    "RouteReplacement",
    "RouterExtensionPlan",
    "build_identity_module_routes",
    "build_identity_route_set",
    "current_user_state",
    "module_routes",
    "normalise_return_to",
]
